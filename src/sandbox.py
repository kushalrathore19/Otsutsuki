import os
import subprocess
import resource
import logging
import shlex
from exceptions import SandboxError

def set_limits(mem_limit_mb: int):
    try:
        mem_limit = mem_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
    except Exception as e:
        logging.warning(f"Failed to set RLIMIT_AS sandbox limit: {e}")
        
    try:
        if hasattr(resource, 'RLIMIT_NPROC'):
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    except Exception as e:
        logging.warning(f"Failed to set RLIMIT_NPROC sandbox limit: {e}")

def check_paths(cmd: str, cwd: str, target_repo: str = None) -> tuple[bool, str]:
    try:
        parts = shlex.split(cmd)
    except Exception:
        parts = cmd.split()
    for part in parts:
        if "=" in part:
            part = part.split("=", 1)[1]
            
        lower_part = part.lower()
        if "secret" in lower_part or "credential" in lower_part or part in [".env", ".git/config", ".git/credentials"] or part.endswith("/.env") or part.endswith("/.git/config") or part.endswith("/.git/credentials"):
            return False, f"Error: Security violation - Path '{part}' is a sensitive repository file."
            
        if part.startswith("/") or "../" in part:
            if any(part.startswith(p) for p in ["/tmp/", "/dev/null", "/bin/", "/usr/bin/"]):
                continue
            resolved = os.path.abspath(os.path.join(cwd, part))
            cwd_real = os.path.abspath(target_repo) if target_repo else os.path.abspath(cwd)
            if not resolved.startswith(cwd_real):
                return False, f"Error: Security violation - Path '{part}' resolves outside the repository root."
    return True, ""

def run_sandboxed(cmd: str, cwd: str, timeout: int, mem_limit_mb: int, target_repo: str = None) -> dict:
    is_valid, err_msg = check_paths(cmd, cwd, target_repo)
    if not is_valid:
        return {"exit_code": 1, "stdout": "", "stderr": err_msg}
    safe_env = os.environ.copy()
    if "AI_API_KEY" in safe_env:
        del safe_env["AI_API_KEY"]
    if subprocess.run("unshare --net true", shell=True, capture_output=True).returncode == 0:
        cmd = f"unshare --net {cmd}"
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, env=safe_env,
            preexec_fn=lambda: set_limits(mem_limit_mb), timeout=timeout, capture_output=True, text=True
        )
        return {"exit_code": result.returncode, "stdout": result.stdout if result.stdout else "", "stderr": result.stderr if result.stderr else ""}
    except subprocess.TimeoutExpired as e:
        return {"exit_code": 124, "stdout": e.stdout if e.stdout else "", "stderr": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        logging.error(f"Sandbox execution error: {e}")
        raise SandboxError(f"Sandbox execution error: {e}") from e
