import os
import subprocess
import resource

def set_limits(mem_limit_mb: int):
    try:
        mem_limit = mem_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        if hasattr(resource, 'RLIMIT_NPROC'):
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    except Exception:
        pass

def run_sandboxed(cmd: str, cwd: str, timeout: int, mem_limit_mb: int) -> dict:
    safe_env = os.environ.copy()
    if "AI_API_KEY" in safe_env:
        del safe_env["AI_API_KEY"]
        
    # Note: strict path-confinement enforcement is a known gap, not currently implemented.
    if subprocess.run("unshare --net true", shell=True, capture_output=True).returncode == 0:
        cmd = f"unshare --net {cmd}"
        
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            env=safe_env,
            preexec_fn=lambda: set_limits(mem_limit_mb),
            timeout=timeout,
            capture_output=True,
            text=True
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout if result.stdout else "",
            "stderr": result.stderr if result.stderr else ""
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": 124,
            "stdout": e.stdout if e.stdout else "",
            "stderr": f"Command timed out after {timeout} seconds"
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e)
        }
