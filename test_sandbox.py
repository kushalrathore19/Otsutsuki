import subprocess
import resource

def set_limits():
    try:
        mem_limit = 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        if hasattr(resource, 'RLIMIT_NPROC'):
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    except Exception as e:
        pass

subprocess.run("echo hello", shell=True, preexec_fn=set_limits)
