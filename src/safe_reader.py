"""Safe, range-limited file reader for LLM tool use.

Reading raw files with ``cat`` is the fastest way to blow an agent's
context budget: minified JavaScript, lockfiles, or 10k-line monoliths
flood the transcript with content the model can not use.  This module
exposes a single tool-facing function, :func:`safe_read_file`, which:

* refuses binary and minified files up front (heuristics, documented
  below) instead of dumping them into the conversation;
* mandates range-based reading -- every call returns at most
  ``MAX_LINES_PER_READ`` lines, even if the caller asked for more;
* enforces a strict character budget per call, trimming from the end and
  saying so instead of silently truncating;
* prints 1-based line numbers next to every line, so the model can quote
  exact context when it later generates a unified diff.

Like :mod:`patcher`, it never raises for tool-facing problems: every
failure is returned as a parseable ``READ_ERROR: ...`` string.
"""

import logging
import os

__all__ = ["safe_read_file", "DEFAULT_MAX_CHARS", "MAX_LINES_PER_READ"]

ERROR_PREFIX = "READ_ERROR"

# Default per-call character budget (header included).
DEFAULT_MAX_CHARS = 20_000
# Hard cap on lines returned per call, regardless of the requested range.
MAX_LINES_PER_READ = 400
# Never read more than this many bytes to classify a file.
_SAMPLE_BYTES = 262_144
# Refuse files larger than this outright.
MAX_FILE_BYTES = 10 * 1024 * 1024
# Minification heuristics: machine-generated files have absurdly long
# lines.  Reject when the *average* line length exceeds the first
# threshold, or when any sampled line exceeds the second (catches mostly
# normal files with one embedded blob, e.g. lockfiles or inline JSON).
MINIFIED_AVG_LINE_LEN = 256
MINIFIED_MAX_LINE_LEN = 2_000

HEADER = "[Otsutsuki safe_read]"


def _is_binary(sample: bytes) -> bool:
    """Heuristic binary sniff: a NUL byte in the first sample means binary."""
    return b"\x00" in sample


def _minified_reason(sample: str) -> str | None:
    """Return a reason string if the sample looks minified, else None."""
    lines = sample.split("\n")
    n_lines = len(lines)
    if n_lines == 0:
        return None
    avg = len(sample) / n_lines
    longest = max(len(ln) for ln in lines)
    if avg > MINIFIED_AVG_LINE_LEN:
        return (
            f"average line length is {avg:.0f} chars "
            f"(> {MINIFIED_AVG_LINE_LEN}); this looks minified/machine-generated"
        )
    if longest > MINIFIED_MAX_LINE_LEN:
        return (
            f"longest line is {longest} chars (> {MINIFIED_MAX_LINE_LEN}); "
            "this file likely embeds a minified or data blob"
        )
    return None


def _coerce_line(value: object, name: str) -> int | str:
    """Coerce a line argument (possibly a string from JSON args) to int."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return f"{ERROR_PREFIX}: {name} must be an integer, got a boolean."
    try:
        return int(value)
    except (TypeError, ValueError):
        return f"{ERROR_PREFIX}: {name} must be an integer, got {value!r}."


def safe_read_file(
    filepath: str,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    repo_root: str | None = None,
) -> str:
    """Read a bounded, line-numbered slice of a text file.

    Args:
        filepath: Path to the file (absolute, or relative to ``repo_root``).
        start_line: 1-based first line to read; values below 1 are clamped.
        end_line: 1-based *inclusive* last line.  Defaults to a window of
            ``MAX_LINES_PER_READ`` lines starting at ``start_line``.  Any
            window is clamped to ``MAX_LINES_PER_READ`` lines -- whole-file
            dumps are not possible by design.
        max_chars: Maximum characters of the returned string (header
            included).  The range is trimmed from the end to fit and a
            note is appended when that happens.
        repo_root: When given, ``filepath`` must resolve inside this
            directory (symlinks resolved) and may not touch ``.git`` or
            ``.env`` files.

    Returns:
        A header line describing file/range, then the numbered lines, or a
        single ``READ_ERROR: ...`` string explaining the refusal.  Never
        raises for tool-facing problems.
    """
    if not isinstance(filepath, str) or not filepath.strip():
        return f"{ERROR_PREFIX}: no filepath given. Pass the repo-relative path, e.g. 'src/main.py'."

    start = _coerce_line(start_line, "start_line")
    if isinstance(start, str):
        return start
    if end_line is not None:
        end = _coerce_line(end_line, "end_line")
        if isinstance(end, str):
            return end
    else:
        end = None

    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return f"{ERROR_PREFIX}: max_chars must be an integer, got {max_chars!r}."
    if max_chars < 200:
        return f"{ERROR_PREFIX}: max_chars must be >= 200 (got {max_chars}); tiny budgets cannot carry useful code."

    root = os.path.realpath(repo_root) if repo_root else None
    path = filepath if os.path.isabs(filepath) else os.path.join(root or os.getcwd(), filepath)
    resolved = os.path.realpath(path)

    if root is not None:
        inside = resolved == root or resolved.startswith(root + os.sep)
        if not inside:
            return f"{ERROR_PREFIX}: '{filepath}' resolves outside the repository root; only repo files are readable."
    parts = resolved.split(os.sep)
    if ".git" in parts:
        return f"{ERROR_PREFIX}: reading git internals ('{filepath}') is not allowed; use run_bash with 'git show'/'git log' instead."
    if os.path.basename(resolved) == ".env":
        return f"{ERROR_PREFIX}: '{filepath}' is a sensitive environment file and cannot be read."

    try:
        if not os.path.exists(resolved):
            return f"{ERROR_PREFIX}: file not found: '{filepath}'. Check the path (ls via run_bash helps) and retry."
        if os.path.isdir(resolved):
            return f"{ERROR_PREFIX}: '{filepath}' is a directory, not a file."
        if os.path.getsize(resolved) > MAX_FILE_BYTES:
            return f"{ERROR_PREFIX}: '{filepath}' exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB; too large to read, locate the relevant part with grep instead."
        with open(resolved, "rb") as f:
            sample_bytes = f.read(_SAMPLE_BYTES)
    except OSError as exc:
        return f"{ERROR_PREFIX}: cannot access '{filepath}': {exc}."

    if _is_binary(sample_bytes):
        return f"{ERROR_PREFIX}: '{filepath}' appears to be binary (NUL bytes detected); reading it into context is pointless."

    sample = sample_bytes.decode("utf-8", errors="replace")
    if sample == "":
        return f"{HEADER} file={filepath} total_lines=0 (empty file)"
    reason = _minified_reason(sample)
    if reason:
        return (
            f"{ERROR_PREFIX}: '{filepath}' rejected as minified/machine-generated: {reason}. "
            "Do not load this file into context; work on targeted excerpts found via grep instead."
        )

    lines = sample.split("\n")
    if sample.endswith("\n"):
        lines.pop()  # trailing newline is not a 0-length final line
    total = len(lines)

    start = max(1, start)
    if start > total:
        return (
            f"{ERROR_PREFIX}: start_line {start} is beyond EOF ('{filepath}' has {total} lines). "
            f"Retry with start_line <= {total}."
        )
    if end is not None and end < start:
        return f"{ERROR_PREFIX}: end_line ({end}) is before start_line ({start}); pass an inclusive end_line >= start_line."

    width = MAX_LINES_PER_READ
    if end is None:
        end = min(total, start + width - 1)
        clamped_range = (start + width - 1) < total
    else:
        requested_end = end
        end = min(end, start + width - 1, total)
        clamped_range = requested_end > end

    selected = list(enumerate(lines[start - 1:end], start=start))

    header = (
        f"{HEADER} file={filepath} total_lines={total} "
        f"showing={selected[0][0]}-{selected[-1][0]}"
    )
    note = ""
    if clamped_range:
        note = (
            f"\n[NOTE: range clamped to {width} lines; "
            f"re-read with start_line={end + 1} for the next chunk.]"
        )

    body_parts = [f"{ln:5d} | {text}" for ln, text in selected]

    def _render_len(hdr: str, parts: list[str], nte: str) -> int:
        joined = len(hdr) + 1 + sum(len(p) + 1 for p in parts) - 1 + len(nte)
        return joined

    # Enforce the character budget by dropping lines from the end; a single
    # oversized line is hard-cut so even pathological input stays bounded.
    trimmed = False
    while len(body_parts) > 1 and _render_len(header, body_parts, note) > max_chars:
        body_parts.pop()
        trimmed = True

    if trimmed:
        first_ln = selected[0][0]
        last_ln = selected[len(body_parts) - 1][0]
        header = f"{HEADER} file={filepath} total_lines={total} showing={first_ln}-{last_ln}"
        note = (
            f"\n[TRUNCATED: output exceeded max_chars={max_chars}; showed lines "
            f"{first_ln}-{last_ln}. Re-read with a narrower range to continue.]"
        )
        # The longer banner can push a couple more lines out; keep >= 1.
        while len(body_parts) > 1 and _render_len(header, body_parts, note) > max_chars:
            body_parts.pop()
            last_ln = selected[len(body_parts) - 1][0]
            header = f"{HEADER} file={filepath} total_lines={total} showing={first_ln}-{last_ln}"
            note = (
                f"\n[TRUNCATED: output exceeded max_chars={max_chars}; showed lines "
                f"{first_ln}-{last_ln}. Re-read with a narrower range to continue.]"
            )

    if _render_len(header, body_parts, note) > max_chars:
        first_ln, first_text = selected[0]
        budget = max(1, max_chars - len(header) - 60)
        body_parts = [f"{first_ln:5d} | {first_text[:budget]} [line truncated]"]
        note = f"\n[TRUNCATED: single line exceeded max_chars={max_chars}.]"

    output = header + "\n" + "\n".join(body_parts) + note
    return output[:max_chars]
