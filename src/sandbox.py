"""Sandboxed bash execution for the Otsutsuki harness.

The agent's only general-purpose tool is ``run_bash``; this module is what
makes that safe to hand to an LLM.  Hardening layers, in the order they
run:

1. **Destructive-command blacklist** -- a curated set of regexes matching
   system-wide ``rm``/``mv``, ``.git`` deletion, disk formatting, raw
   device writes, fork bombs, privilege escalation, remote-script piping
   and power control.  Matched commands are refused *before* execution
   with exit code 126 and a one-line reason the model can learn from.
2. **Path confinement** (:func:`check_paths`) -- path-like tokens are
   resolved (symlinks included) and must land inside ``target_repo``.
   Sensitive files (``.env``, ``.git/config``, credentials) are refused
   outright; a small allowlist covers system executables and ``/tmp``
   scratch space.
3. **Secret-free environment** -- API keys / tokens / secrets are stripped
   from the child environment so no subprocess can leak them.
4. **Resource limits** (best-effort, never fatal) -- address-space and
   process-count rlimits *that never lower below the current soft limit*,
   plus a CPU-time backstop.  (The previous implementation clamped
   ``RLIMIT_NPROC`` to 256 unconditionally; on busy dev machines that is
   below the caller's existing process count and every child died with
   ``fork: Resource temporarily unavailable`` -- exit 128.)
5. **Wall-clock timeout with process-group kill** -- the command runs in
   its own session; on timeout the *whole process group* receives SIGKILL,
   so grandchildren (build daemons, sleep chains) cannot outlive the call
   and hang the environment.
6. **Output caps** -- stdout/stderr are truncated before returning, so a
   chatty command cannot flood the model's context.

The module never raises for tool-facing failures; :func:`run_bash` always
returns a result dict.
"""

import logging
import os
import re
import shlex
import signal
import subprocess
import sys
from functools import partial

__all__ = ["run_bash", "run_sandboxed", "check_paths", "set_limits"]

DEFAULT_TIMEOUT_S = 30
DEFAULT_MEM_LIMIT_MB = 1024
# Per-stream cap on returned output; protects the LLM context window.
DEFAULT_MAX_OUTPUT_CHARS = 20_000

# Environment variables never handed to a subprocess.
_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|credential)"
)
_ALWAYS_STRIPPED_ENV = {"AI_API_KEY"}

# Path prefixes allowed to appear in commands even though they sit outside
# the target repository: system executables and scratch/null devices.
_OUTSIDE_ALLOWLIST_PREFIXES = (
    "/bin/", "/usr/bin/", "/usr/local/bin/", "/sbin/", "/usr/sbin/",
    "/opt/homebrew/",
    "/tmp/", "/private/tmp/",
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/urandom", "/dev/random",
)

# (pattern, reason) -- reasons are shown to the model verbatim.
DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\b[^;&|]*\s/(?:\*|\s|$)"),
        "recursive rm targeting the filesystem root",
    ),
    (
        re.compile(r"\brm\b[^;&|]*\s(?:~|~/\*|\$HOME|\$\{HOME\})(?:\s|/|$)"),
        "recursive rm targeting the home directory",
    ),
    (
        re.compile(r"\b(?:rm|rmdir|unlink|mv)\b[^;&|]*[\"']?\.git(?:[\"']?(?:\s|/|$))"),
        "deletion or relocation of git internals (.git)",
    ),
    (
        re.compile(r"\bmkfs(?:\.\w+)?\b"),
        "filesystem formatting",
    ),
    (
        re.compile(r"\bdd\b[^;&|]*\bof=/dev/"),
        "raw device write via dd",
    ),
    (
        re.compile(r":\(\)\s*\{"),
        "fork bomb",
    ),
    (
        re.compile(r"\b(?:sudo|su)\s"),
        "privilege escalation",
    ),
    (
        re.compile(r"\b(curl|wget)\b[^;&|]*\|\s*(?:ba|z|da|fi)?sh\b"),
        "piping remote content into a shell",
    ),
    (
        re.compile(r"\bchmod\s+(?:-R\s+)?[0-7]{3,4}\s+/(?:\s|$)"),
        "recursive permission change on the filesystem root",
    ),
    (
        re.compile(r"\b(?:shutdown|poweroff|halt|reboot|diskutil\s+erase|diskpart)\b"),
        "system power/disk control",
    ),
]

_netns_available: bool | None = None


# ---------------------------------------------------------------------------
# Resource limits (best-effort)
# ---------------------------------------------------------------------------

def set_limits(mem_limit_mb: int, cpu_seconds: int | None = None) -> None:
    """Apply child-process rlimits.  Best-effort: logs and continues.

    Two rules matter on real dev machines:
    * ``RLIMIT_AS`` is skipped on macOS, where setrlimit rejects it.
    * ``RLIMIT_NPROC`` is never lowered below the *current* soft limit --
      clamping it to a fixed small number starves ``/bin/sh`` of forks
      when the user already runs many processes (the exit-128 failure).
    """
    import resource

    if sys.platform != "darwin":
        try:
            mem_bytes = int(mem_limit_mb) * 1024 * 1024
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard == resource.RLIM_INFINITY or mem_bytes <= hard:
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, hard))
            else:
                logging.warning("RLIMIT_AS %dMB exceeds hard limit; not applied.", mem_limit_mb)
        except Exception as exc:  # noqa: BLE001 - rlimits are best-effort
            logging.warning("Failed to set RLIMIT_AS sandbox limit: %s", exc)

    try:
        if hasattr(resource, "RLIMIT_NPROC"):
            soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            if soft != resource.RLIM_INFINITY:
                new_soft = max(soft, 256)
                if hard != resource.RLIM_INFINITY:
                    new_soft = min(new_soft, hard)
                resource.setrlimit(resource.RLIMIT_NPROC, (new_soft, hard))
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to set RLIMIT_NPROC sandbox limit: %s", exc)

    if cpu_seconds and cpu_seconds > 0:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to set RLIMIT_CPU sandbox limit: %s", exc)


# ---------------------------------------------------------------------------
# Static command analysis
# ---------------------------------------------------------------------------

def check_paths(cmd: str, cwd: str, target_repo: str | None = None) -> tuple[bool, str]:
    """Reject commands that reference paths outside the target repository.

    Every path-like token is resolved (symlinks included) and must land
    inside ``target_repo`` (defaulting to ``cwd``).  System executables,
    ``/tmp`` scratch and ``/dev`` null devices are allowlisted; sensitive
    files (``.env``, ``.git/config``, credentials) are refused outright.

    Returns:
        (True, "") when the command passes, else (False, reason).
    """
    try:
        parts = shlex.split(cmd)
    except Exception:  # malformed quoting; fall back to whitespace split
        parts = cmd.split()

    repo_root = os.path.realpath(target_repo or cwd)

    for part in parts:
        if "=" in part and not part.startswith("/"):
            part = part.split("=", 1)[1] or part

        lower = part.lower()
        sensitive_suffixes = (
            ".env", ".git/config", ".git/credentials", ".git-credentials",
        )
        if (
            "secret" in lower or "credential" in lower
            or part in (".env", ".git/config", ".git/credentials")
            or part.endswith(sensitive_suffixes)
        ):
            return False, (
                f"Error: Security violation - Path '{part}' is a sensitive repository file."
            )

        candidate = os.path.expanduser(part) if part.startswith("~") else part
        if not candidate.startswith("/") and "../" not in candidate:
            continue  # relative path inside the repo; cwd confinement covers it

        if candidate.startswith(_OUTSIDE_ALLOWLIST_PREFIXES):
            continue

        resolved = os.path.realpath(
            candidate if os.path.isabs(candidate) else os.path.join(cwd, candidate)
        )
        if resolved != repo_root and not resolved.startswith(repo_root + os.sep):
            return False, (
                f"Error: Security violation - Path '{part}' resolves outside the repository root."
            )
    return True, ""


def destructive_reason(cmd: str) -> str | None:
    """Return the blacklist reason if ``cmd`` matches, else None."""
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if pattern.search(cmd):
            return reason
    return None


def _build_env() -> dict[str, str]:
    """Child environment with secrets stripped."""
    env = os.environ.copy()
    for name in list(env):
        if name in _ALWAYS_STRIPPED_ENV or _SECRET_ENV_RE.search(name):
            env.pop(name, None)
    return env


def _netns_prefix() -> str:
    """'unshare --net ' on Linux when possible, else '' (best-effort)."""
    global _netns_available
    if _netns_available is None:
        try:
            probe = subprocess.run(
                "unshare --net true", shell=True, capture_output=True, timeout=10,
            )
            _netns_available = probe.returncode == 0
        except Exception:  # noqa: BLE001
            _netns_available = False
    return "unshare --net " if _netns_available else ""


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the command's whole process group (it owns a session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public executor
# ---------------------------------------------------------------------------

def run_bash(
    cmd: str,
    *,
    cwd: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB,
    target_repo: str | None = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> dict:
    """Execute ``cmd`` inside the sandbox and return a structured result.

    Args:
        cmd: The bash command line (executed via ``/bin/sh``).
        cwd: Working directory for the command (the repository root).
        timeout_s: Strict wall-clock limit; the entire process group is
            SIGKILLed when it elapses.
        mem_limit_mb: Address-space cap attempted for the child tree.
        target_repo: Confinement root; path arguments must resolve inside
            it.  Defaults to ``cwd``.
        max_output_chars: Per-stream cap on returned stdout/stderr.

    Returns:
        dict with keys ``exit_code``, ``stdout``, ``stderr`` plus
        ``timed_out``, ``truncated`` and ``blocked``.  Security refusals
        use exit code 126 (blacklist) or 1 (path violation) with the
        reason in ``stderr``.  Never raises for tool-facing failures.
    """
    result: dict = {
        "exit_code": 1, "stdout": "", "stderr": "",
        "timed_out": False, "truncated": False, "blocked": False,
    }

    if not isinstance(cmd, str) or not cmd.strip():
        result["exit_code"] = 2
        result["stderr"] = "Error: empty command."
        return result

    reason = destructive_reason(cmd)
    if reason:
        result["exit_code"] = 126
        result["blocked"] = True
        result["stderr"] = (
            f"Error: Security violation - command refused by the harness ({reason}). "
            "Use a safer command that only operates inside the target repository."
        )
        logging.warning("Sandbox blacklist hit: %s || cmd: %s", reason, cmd[:200])
        return result

    is_valid, err_msg = check_paths(cmd, cwd, target_repo)
    if not is_valid:
        result["exit_code"] = 1
        result["blocked"] = True
        result["stderr"] = err_msg
        logging.warning("Sandbox path rejection: %s || cmd: %s", err_msg, cmd[:200])
        return result

    env = _build_env()
    # CPU-time backstop: generous (wall-clock is the strict limit) because
    # CPU seconds accumulate across cores for parallel builds/tests.
    cpu_backstop = max(4 * int(timeout_s), 120)
    preexec = partial(set_limits, mem_limit_mb, cpu_backstop) if os.name == "posix" else None
    full_cmd = _netns_prefix() + cmd

    try:
        proc = subprocess.Popen(
            full_cmd,
            shell=True,
            cwd=cwd,
            env=env,
            preexec_fn=preexec,
            start_new_session=(os.name == "posix"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:  # spawn failure (bad cwd, resource exhaustion)
        result["exit_code"] = 125
        result["stderr"] = f"Error: harness failed to start command: {exc}"
        logging.error("Sandbox spawn failure: %s", exc)
        return result

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # fully wedged pipes; give up on the leftovers
            stdout, stderr = "", ""
        result["timed_out"] = True
    except Exception as exc:  # noqa: BLE001 - communication failure
        _kill_process_group(proc)
        result["exit_code"] = 125
        result["stderr"] = f"Error: harness lost contact with the command: {exc}"
        logging.error("Sandbox communication failure: %s", exc)
        return result

    result["exit_code"] = 124 if timed_out else (proc.returncode if proc.returncode is not None else 125)
    result["stdout"] = stdout or ""
    result["stderr"] = stderr or ""
    if timed_out:
        result["stderr"] += (
            f"\n[Otsutsuki] Command exceeded the {timeout_s}s wall-clock limit and was "
            "killed (whole process group). Split it into smaller steps or raise TIMEOUT_SECONDS."
        )

    for key in ("stdout", "stderr"):
        if len(result[key]) > max_output_chars:
            result[key] = (
                result[key][:max_output_chars]
                + f"\n[Otsutsuki] {key} truncated at {max_output_chars} chars."
            )
            result["truncated"] = True
    return result


def run_sandboxed(
    cmd: str,
    cwd: str,
    timeout: int = DEFAULT_TIMEOUT_S,
    mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB,
    target_repo: str | None = None,
) -> dict:
    """Backwards-compatible wrapper around :func:`run_bash`.

    Keeps the pre-existing positional signature used by the orchestrator
    and the test-suite mocks.
    """
    return run_bash(
        cmd,
        cwd=cwd,
        timeout_s=timeout,
        mem_limit_mb=mem_limit_mb,
        target_repo=target_repo,
    )
