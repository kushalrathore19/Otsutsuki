"""Fuzzy unified-diff applier for LLM-generated patches.

LLM-generated diffs routinely contain slightly wrong context lines, drifted
line numbers, tab/space confusion, markdown fences, or miscounted hunk
headers -- all of which make the Unix ``patch`` utility hard-fail and burn
agent iteration budget.  This module parses such diffs tolerantly and
applies each hunk by locating its old content in the target file using
staged matching:

    stage 0  exact text match
    stage 1  whitespace-normalised match (expandtabs + rstrip)
    stage 2  indentation-insensitive match (strip every line)
    stage 3  fuzzy match via difflib.SequenceMatcher (ratio >= threshold)

The hunk's stated start line is only a search *hint*: the matcher searches
outward from the hint across the whole file, so stale line numbers do not
break application.  Failed hunks never raise -- every failure is reported
as a concise, machine-parseable ``PATCH_ERROR: ...`` string so the calling
LLM can read it and self-correct on the next attempt.

Public API:
    apply_patch_diff(diff_text, repo_root, ...) -> str   (OK:/PATCH_ERROR:)
"""

import difflib
import logging
import os
import re
from dataclasses import dataclass, field

__all__ = ["apply_patch_diff", "FilePatch", "Hunk", "OK_PREFIX", "ERROR_PREFIX"]

OK_PREFIX = "OK"
ERROR_PREFIX = "PATCH_ERROR"

# Match quality below this ratio is rejected in the fuzzy (stage 3) pass.
DEFAULT_MIN_FUZZY_RATIO = 0.85
# Files larger than this are refused outright (protects memory and sanity).
MAX_FILE_BYTES = 5 * 1024 * 1024

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FENCE_RE = re.compile(r"^```(\w+)?\s*$")
_GIT_HEADER_RE = re.compile(r'^diff --git a/(.+?) b/(.+?)$')

# Lines that appear between file headers and hunks in real git diffs and
# must be skipped rather than treated as hunk content or prose.
_METADATA_PREFIXES = (
    "index ", "new file mode", "deleted file mode", "old mode", "new mode",
    "similarity index", "dissimilarity index", "rename from", "rename to",
    "copy from", "copy to", "Binary files", "GIT binary patch", "literal ",
)


@dataclass
class Hunk:
    """One ``@@`` hunk.  ``lines`` holds (op, text) with op in {' ', '-', '+', '\\'}."""

    old_start: int | None          # 1-based, from the header; None if missing/mangled
    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def old_lines(self) -> list[str]:
        """Lines the hunk expects to find in the file (context + deletions)."""
        return [text for op, text in self.lines if op in (" ", "-")]

    @property
    def new_lines(self) -> list[str]:
        """Lines the hunk produces (context + additions)."""
        return [text for op, text in self.lines if op in (" ", "+")]

    @property
    def old_count(self) -> int:
        return len(self.old_lines)

    @property
    def new_count(self) -> int:
        return len(self.new_lines)


@dataclass
class FilePatch:
    """All hunks for one target file, as declared by the diff headers."""

    old_path: str | None   # None means "/dev/null" (new file)
    new_path: str | None   # None means "/dev/null" (file deletion)
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def is_new_file(self) -> bool:
        return self.old_path is None

    @property
    def is_file_deletion(self) -> bool:
        return self.new_path is None


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm_loose(line: str) -> str:
    """Stage-1 normalisation: tabs expanded, trailing whitespace ignored."""
    return line.expandtabs(4).rstrip()


def _norm_tight(line: str) -> str:
    """Stage-2 normalisation: indentation and trailing whitespace ignored."""
    return line.expandtabs(4).strip()


# ---------------------------------------------------------------------------
# Diff parsing (tolerant)
# ---------------------------------------------------------------------------

def _clean_path(raw: str) -> str | None:
    """Strip timestamps/quotes from a header path; map /dev/null to None."""
    path = raw.split("\t")[0].strip().strip('"').strip("'")
    if not path or path == "/dev/null":
        return None
    return path


def _is_probable_file_header(line: str, next_line: str | None, hunk_open: bool) -> bool:
    """A '--- ' line only counts as a file header when followed by '+++ '.

    Inside an open hunk we additionally require the conventional a/ b/
    /dev/null prefixes, so that deleting a file line that literally starts
    with '--- ' is not mistaken for a new file section.
    """
    if not line.startswith("--- ") or next_line is None or not next_line.startswith("+++ "):
        return False
    if not hunk_open:
        return True
    target = _clean_path(line[4:]) or ""
    return target.startswith(("a/", "b/")) or target == ""


def _parse_diff(text: str) -> tuple[list[FilePatch] | None, str | None]:
    """Parse LLM diff text into FilePatch objects.

    Returns (patches, None) on success or (None, error_string) when no
    usable diff structure can be recovered.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.splitlines() if not _FENCE_RE.match(ln.strip())]

    patches: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    declared_old: int | None = None
    declared_new: int | None = None
    git_paths: tuple[str, str] | None = None

    def flush_hunk() -> None:
        nonlocal hunk, declared_old, declared_new
        if hunk is not None and hunk.lines and cur is not None:
            cur.hunks.append(hunk)
        hunk = None
        declared_old = declared_new = None

    def flush_patch() -> None:
        nonlocal cur, git_paths
        flush_hunk()
        if cur is not None and cur.hunks:
            patches.append(cur)
        cur = None
        git_paths = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < n else None

        if line.startswith("diff --git "):
            flush_patch()
            m = _GIT_HEADER_RE.match(line)
            if m:
                git_paths = (m.group(1), m.group(2))
            i += 1
            continue

        if _is_probable_file_header(line, next_line, hunk_open=hunk is not None):
            flush_patch()
            cur = FilePatch(
                old_path=_clean_path(line[4:]),
                new_path=_clean_path(next_line[4:]),
                hunks=[],
            )
            i += 2
            continue

        if line.startswith("@@"):
            flush_hunk()
            m = _HUNK_HEADER_RE.match(line)
            old_start = int(m.group(1)) if m else None
            if m:
                declared_old = int(m.group(2)) if m.group(2) is not None else 1
                declared_new = int(m.group(4)) if m.group(4) is not None else 1
            else:
                declared_old = declared_new = None
            hunk = Hunk(old_start=old_start, lines=[])
            if cur is None:
                if git_paths:
                    cur = FilePatch(old_path=git_paths[0], new_path=git_paths[1], hunks=[])
                else:
                    return None, (
                        f"{ERROR_PREFIX}: hunk found but no file header. "
                        "Every hunk must be preceded by '--- a/<path>' and '+++ b/<path>' lines."
                    )
            i += 1
            continue

        if hunk is not None:
            if line.startswith("\\"):
                hunk.lines.append(("\\", line))
                i += 1
                continue
            if line.startswith((" ", "+", "-")):
                hunk.lines.append((line[0], line[1:]))
                i += 1
                continue
            consumed_old = sum(1 for op, _ in hunk.lines if op in (" ", "-"))
            consumed_new = sum(1 for op, _ in hunk.lines if op in (" ", "+"))
            counts_satisfied = (
                declared_old is None or declared_new is None
                or consumed_old < declared_old or consumed_new < declared_new
            )
            if counts_satisfied:
                # LLM diffs frequently drop the leading space (or even the
                # leading '+') on content lines.  In a pure-addition hunk
                # (old side empty, as in new-file diffs) a bare line can
                # only be an addition; in a pure-deletion hunk it can only
                # be a deletion; otherwise it is prefix-less context.
                if declared_old == 0:
                    hunk.lines.append(("+", line))
                elif declared_new == 0:
                    hunk.lines.append(("-", line))
                else:
                    hunk.lines.append((" ", line))
                i += 1
                continue
            flush_hunk()  # counts complete -> this line ends the hunk (prose/next section)
            # fall through for this line to the junk handling below

        if line.startswith(_METADATA_PREFIXES):
            i += 1
            continue

        if hunk is not None and line.strip():
            flush_hunk()
        i += 1

    flush_patch()

    if not patches:
        return None, (
            f"{ERROR_PREFIX}: no valid unified diff found in input. "
            "Emit a proper unified diff with '--- a/<path>', '+++ b/<path>' "
            "and '@@ ... @@' hunk headers."
        )
    return patches, None


# ---------------------------------------------------------------------------
# Hunk location (staged matching)
# ---------------------------------------------------------------------------

def _positions_by_distance(hint: int, lo: int, hi: int):
    """Yield candidate start positions in [lo, hi], nearest to hint first."""
    if hi < lo:
        return
    for dist in range(0, hi - lo + 1):
        left = hint - dist
        if left >= lo:
            yield left
        right = hint + dist
        if right <= hi and right != left:
            yield right


def _find_exact(file_norm: list[str], old_norm: list[str], hint: int) -> int | None:
    """Return the start index whose normalised window equals old_norm, or None."""
    m = len(old_norm)
    if m == 0:
        return hint
    limit = len(file_norm) - m
    for pos in _positions_by_distance(hint, 0, max(0, limit)):
        if file_norm[pos:pos + m] == old_norm:
            return pos
    return None


def _find_fuzzy(
    file_norm: list[str],
    old_norm: list[str],
    hint: int,
    min_ratio: float,
) -> tuple[int, float] | None:
    """Best SequenceMatcher window match, using a cheap line-prefilter.

    Returns (start_index, ratio) or None when nothing clears min_ratio.
    """
    m = len(old_norm)
    if m == 0:
        return None
    limit = len(file_norm) - m
    if limit < 0:
        return None

    old_set = set(old_norm)
    scored: list[tuple[float, int, int]] = []   # (-prefilter, |pos-hint|, pos)
    for pos in range(0, limit + 1):
        window = file_norm[pos:pos + m]
        hits = sum(1 for ln in window if ln in old_set)
        prefilter = hits / m
        if prefilter < 0.5:
            continue
        scored.append((-prefilter, abs(pos - hint), pos))
    scored.sort()
    if not scored:
        return None

    sm = difflib.SequenceMatcher(None, old_norm, None, autojunk=False)
    best_pos, best_ratio = None, 0.0
    for _, _, pos in scored[:25]:
        sm.set_seq2(file_norm[pos:pos + m])
        ratio = sm.ratio()
        if ratio > best_ratio:
            best_pos, best_ratio = pos, ratio
    if best_pos is None or best_ratio < min_ratio:
        return None
    return best_pos, best_ratio


def _build_replacement_exact(file_lines: list[str], pos: int, hunk: Hunk) -> list[str]:
    """Rebuild the hunk region keeping the *file's* text for context lines.

    Only '+' lines (and dropped '-' lines) change, so whitespace the diff
    got slightly wrong is never written back into the file.
    """
    out: list[str] = []
    oi = 0
    for op, text in hunk.lines:
        if op == "+":
            out.append(text)
        elif op == "-":
            oi += 1
        elif op == " ":
            out.append(file_lines[pos + oi])
            oi += 1
        # "\\" ("no newline at file end") markers are handled at write time.
    return out


def _build_replacement_fuzzy(
    file_lines: list[str], pos: int, m: int, hunk: Hunk
) -> list[str]:
    """Splice the hunk into an imperfectly matched window.

    Lines aligned to context are kept verbatim from the file, '-' lines are
    dropped, '+' lines inserted in hunk order, and window lines the hunk
    never accounted for are preserved rather than destroyed.
    """
    window_raw = file_lines[pos:pos + m]
    old_ops = [(op, text) for op, text in hunk.lines if op in (" ", "-")]
    sm = difflib.SequenceMatcher(
        None,
        [_norm_tight(ln) for ln in window_raw],
        [_norm_tight(text) for _, text in old_ops],
        autojunk=False,
    )
    old_to_win: dict[int, int] = {}
    for tag, w1, w2, o1, o2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(w2 - w1):
                old_to_win[o1 + k] = w1 + k

    out: list[str] = []
    wi_cursor = 0
    oi = 0
    for op, text in hunk.lines:
        if op == "+":
            out.append(text)
            continue
        if op == "\\":
            continue
        w = old_to_win.get(oi)
        if w is not None:
            for k in range(wi_cursor, w):          # preserve unmatched file lines
                out.append(window_raw[k])
            wi_cursor = w + 1
            if op == " ":
                out.append(window_raw[w])
        elif op == " ":
            out.append(text)                        # context lost in the file; keep hunk's version
        # unmatched '-' line: treat as deleted
        oi += 1
    for k in range(wi_cursor, len(window_raw)):
        out.append(window_raw[k])
    return out


@dataclass
class _Located:
    pos: int                       # 0-based start of the replaced region
    end: int                       # 0-based exclusive end
    replacement: list[str] | None  # None => hunk already applied, no-op
    detail: str                    # human/LLM-readable application note


def _locate_hunk(
    file_lines: list[str],
    norm_variants: list[list[str]],
    hunk: Hunk,
    hunk_idx: int,
    min_ratio: float,
) -> _Located | str:
    """Find where a hunk belongs in the file.

    Returns a _Located on success or an error string on failure.
    """
    old_lines = hunk.old_lines
    m = len(old_lines)
    hint = (hunk.old_start - 1) if hunk.old_start else 0
    hint = max(0, min(hint, max(0, len(file_lines) - m)))

    # Idempotency: if the *new* content already sits in the file, this hunk
    # was applied by an earlier (possibly crashed) attempt -- treat as done.
    new_norm = [_norm_tight(ln) for ln in hunk.new_lines]
    if new_norm and _find_exact(norm_variants[2], new_norm, hint) is not None:
        pos = _find_exact(norm_variants[2], new_norm, hint)
        return _Located(pos, pos, None, "already applied")

    if m == 0:
        # Pure-addition hunk with no context: only the stated line number
        # can anchor it.  Trust it, clamped to the file bounds.
        if hunk.old_start is None:
            return (
                f"hunk #{hunk_idx}: no context lines and no line number -- "
                "cannot determine insertion point. Include 1-3 unchanged "
                "context lines around additions."
            )
        pos = max(0, min(hunk.old_start - 1, len(file_lines)))
        return _Located(pos, pos, hunk.new_lines, "anchored at stated line")

    old_norm_variants = (
        list(old_lines),                                # stage 0: raw text
        [_norm_loose(l) for l in old_lines],            # stage 1: tabs + rstrip
        [_norm_tight(l) for l in old_lines],            # stage 2: strip() every line
    )
    stage_modes = ("exact", "whitespace-normalised", "indentation-insensitive")
    for stage, file_norm in enumerate(norm_variants):
        pos = _find_exact(file_norm, old_norm_variants[stage], hint)
        if pos is not None:
            return _Located(pos, pos + m, _build_replacement_exact(file_lines, pos, hunk), stage_modes[stage])

    fuzzy = _find_fuzzy(norm_variants[2], [_norm_tight(l) for l in old_lines], hint, min_ratio)
    if fuzzy is not None:
        pos, ratio = fuzzy
        return _Located(
            pos, pos + m, _build_replacement_fuzzy(file_lines, pos, m, hunk),
            f"fuzzy(score={ratio:.2f}, offset={pos - hint:+d})",
        )

    best = _find_fuzzy(norm_variants[2], [_norm_tight(l) for l in old_lines], hint, 0.0)
    best_txt = f"best fuzzy score {best[1]:.2f} < {min_ratio:.2f} near line {best[0] + 1}" if best \
        else "context lines do not appear anywhere in the file"
    return (
        f"hunk #{hunk_idx}: expected context not found ({best_txt}, "
        f"hunk says line {hunk.old_start}). Re-read this file around the "
        "intended change and regenerate the hunk with exact context lines."
    )


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------

def _resolve_target(path: str | None, repo_root: str) -> str | None:
    """Resolve a diff path to an absolute path inside repo_root.

    Returns the absolute path, None for /dev/null, or an error string
    prefixed with 'ERROR:'.
    """
    if path is None:
        return None
    rel = path
    if rel.startswith(("a/", "b/", "i/", "w/")):
        rel = rel[2:]
    root = os.path.realpath(repo_root)
    if os.path.isabs(rel):
        resolved = os.path.realpath(rel)
    else:
        resolved = os.path.realpath(os.path.join(root, rel))
    if resolved != root and not resolved.startswith(root + os.sep):
        return f"ERROR: path '{path}' resolves outside the repository root."
    if (os.sep + ".git") in resolved or resolved.endswith(os.sep + ".git"):
        return f"ERROR: refusing to touch git internals: '{path}'."
    return resolved


# ---------------------------------------------------------------------------
# Per-file application
# ---------------------------------------------------------------------------

def _read_lines(path: str) -> tuple[list[str] | None, bool, str | None]:
    """Read a file into lines. Returns (lines, had_trailing_newline, error)."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, False, f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB; edit it in smaller chunks"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        return None, False, f"cannot read file: {exc}"
    if text == "":
        return [], False, None
    trailing = text.endswith("\n")
    lines = text[:-1].split("\n") if trailing else text.split("\n")
    return lines, trailing, None


def _write_lines(path: str, lines: list[str], trailing: bool) -> str | None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if trailing and lines else ""))
    except OSError as exc:
        return f"cannot write file: {exc}"
    return None


def _apply_file_patch(patch: FilePatch, repo_root: str, min_ratio: float) -> str:
    """Apply one FilePatch. Returns an OK: detail line or a PATCH_ERROR line."""
    raw_label = patch.new_path or patch.old_path or "<unknown>"
    label = raw_label[2:] if raw_label.startswith(("a/", "b/")) else raw_label

    old_resolved = _resolve_target(patch.old_path, repo_root) if not patch.is_new_file else None
    if isinstance(old_resolved, str) and old_resolved.startswith("ERROR"):
        return f"{ERROR_PREFIX}: file '{label}': {old_resolved.removeprefix('ERROR: ')}"
    new_resolved = _resolve_target(patch.new_path, repo_root) if not patch.is_file_deletion else None
    if isinstance(new_resolved, str) and new_resolved.startswith("ERROR"):
        return f"{ERROR_PREFIX}: file '{label}': {new_resolved.removeprefix('ERROR: ')}"

    if patch.is_new_file:
        return _apply_new_file(patch, new_resolved, label)
    return _apply_modification(patch, old_resolved, new_resolved, label, repo_root, min_ratio)


def _apply_new_file(patch: FilePatch, resolved: str | None, label: str) -> str:
    if resolved is None:
        return f"{ERROR_PREFIX}: file '{label}': new-file diff has no target path."
    content_lines: list[str] = []
    for hunk in patch.hunks:
        content_lines.extend(text for op, text in hunk.lines if op == "+")
    no_final_newline = bool(patch.hunks) and patch.hunks[-1].lines and patch.hunks[-1].lines[-1][0] == "\\"

    if os.path.exists(resolved):
        existing, _, err = _read_lines(resolved)
        if err is None and [_norm_tight(l) for l in existing or []] == [_norm_tight(l) for l in content_lines]:
            return f"OK: {label} already has the intended content (idempotent skip)."
        return f"{ERROR_PREFIX}: file '{label}': new-file diff but the file already exists. Read it and emit a modification diff instead."

    err = _write_lines(resolved, content_lines, trailing=not no_final_newline)
    if err:
        return f"{ERROR_PREFIX}: file '{label}': {err}."
    return f"OK: {label} created ({len(content_lines)} lines)."


def _apply_modification(
    patch: FilePatch,
    old_resolved: str | None,
    new_resolved: str | None,
    label: str,
    repo_root: str,
    min_ratio: float,
) -> str:
    path = new_resolved or old_resolved
    if path is None:
        return f"{ERROR_PREFIX}: file '{label}': no target path in diff headers."
    if not os.path.isfile(path):
        return (
            f"{ERROR_PREFIX}: file '{label}': target file does not exist. "
            "If this is meant to create it, use '--- /dev/null' as the old path."
        )

    lines, trailing, err = _read_lines(path)
    if err:
        return f"{ERROR_PREFIX}: file '{label}': {err}."

    norm_variants = [
        list(lines),
        [_norm_loose(l) for l in lines],
        [_norm_tight(l) for l in lines],
    ]

    located: list[tuple[int, _Located]] = []
    for idx, hunk in enumerate(patch.hunks, start=1):
        result = _locate_hunk(lines, norm_variants, hunk, idx, min_ratio)
        if isinstance(result, str):
            return f"{ERROR_PREFIX}: file '{label}': {result}"
        located.append((idx, result))

    # Refuse overlapping regions, then apply bottom-up so earlier edits
    # cannot invalidate later line positions.
    located.sort(key=lambda item: item[1].pos)
    for (idx_a, a), (idx_b, b) in zip(located, located[1:]):
        if b.pos < a.end and a.replacement is not None and b.replacement is not None:
            return f"{ERROR_PREFIX}: file '{label}': hunks #{idx_a} and #{idx_b} overlap; regenerate as one hunk."

    applied_any = False
    details: list[str] = []
    for idx, loc in sorted(located, key=lambda item: item[1].pos, reverse=True):
        if loc.replacement is None:
            details.append(f"hunk#{idx} {loc.detail}")
            continue
        lines[loc.pos:loc.end] = loc.replacement
        applied_any = True
        details.append(f"hunk#{idx} {loc.detail} @L{loc.pos + 1}")

    if patch.is_file_deletion:
        try:
            os.remove(path)
        except OSError as exc:
            return f"{ERROR_PREFIX}: file '{label}': cannot delete file: {exc}."
        return f"OK: {label} deleted. " + "; ".join(details)

    if not applied_any:
        return f"OK: {label} already matches the intended content (idempotent skip; {'; '.join(details)})."

    err = _write_lines(path, lines, trailing=trailing)
    if err:
        return f"{ERROR_PREFIX}: file '{label}': {err}."
    return f"OK: {label}: {'; '.join(details)}."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_patch_diff(
    diff_text: str,
    repo_root: str,
    *,
    min_fuzzy_ratio: float = DEFAULT_MIN_FUZZY_RATIO,
) -> str:
    """Apply a (possibly sloppy) LLM-generated unified diff to a repository.

    Tolerates: markdown fences, stale hunk line numbers, wrong whitespace /
    indentation in context lines, miscounted hunk headers, and prose mixed
    into the diff.  Multi-file diffs and new-file / file-deletion diffs are
    supported.  Application is idempotent: hunks whose result already
    exists are skipped rather than failing.

    Args:
        diff_text: Raw diff text, fences and prose included.
        repo_root: Absolute path to the target repository (confinement root).
        min_fuzzy_ratio: Minimum difflib similarity (0..1) for the fuzzy pass.

    Returns:
        A concise string starting with ``OK`` or ``PATCH_ERROR``.  It never
        raises for LLM-facing problems, so the orchestrator can feed the
        returned string straight back into the model's tool-result channel.
    """
    if not isinstance(diff_text, str) or not diff_text.strip():
        return f"{ERROR_PREFIX}: empty diff. Provide a unified diff (---/+++ headers, @@ hunks)."
    root = os.path.realpath(repo_root)
    if not os.path.isdir(root):
        return f"{ERROR_PREFIX}: repository root '{repo_root}' does not exist."

    try:
        patches, parse_error = _parse_diff(diff_text)
    except Exception as exc:  # never let a parse bug kill the agent loop
        logging.warning("patcher: unexpected parse failure: %s", exc)
        return f"{ERROR_PREFIX}: diff could not be parsed ({exc}). Emit a standard unified diff."
    if parse_error:
        return parse_error

    detail_lines: list[str] = []
    errors: list[str] = []
    for patch in patches:
        try:
            outcome = _apply_file_patch(patch, root, min_fuzzy_ratio)
        except Exception as exc:
            logging.exception("patcher: unexpected apply failure")
            outcome = f"{ERROR_PREFIX}: file '{patch.new_path or patch.old_path}': internal error: {exc}"
        (errors if outcome.startswith(ERROR_PREFIX) else detail_lines).append(outcome)

    if errors:
        return "\n".join(errors)
    return "\n".join(detail_lines)
