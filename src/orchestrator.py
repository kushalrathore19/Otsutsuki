import os
import subprocess
import resource
import json
from openai import OpenAI

SYSTEM_PROMPT = """You are a Proof-First AI Coding Harness agent.
You have exactly one tool available: `run_bash`.
You must navigate the repository yourself using `grep`, `find`, `cat`.
For any code changes, you MUST format them as unified diffs, write them to `/tmp/<n>.patch` (e.g. using `cat << 'EOF' > /tmp/1.patch ... EOF`), and then apply them via the `patch` utility (e.g. `patch -p1 < /tmp/1.patch`).
Never attempt to overwrite files inline or use custom editors.
Your goal is to solve the given task using bash commands.
"""

def set_limits():
    """Resource limit sandbox via preexec_fn."""
    try:
        # Cap memory to 1GB
        mem_limit = 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        # Cap max processes (fallback silently if unsupported on OS)
        if hasattr(resource, 'RLIMIT_NPROC'):
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    except Exception:
        pass # Best effort sandbox

class Orchestrator:
    def __init__(self):
        self.api_key = os.environ.get("AI_API_KEY")
        if not self.api_key:
            raise ValueError("AI_API_KEY environment variable is missing")
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://api.groq.com/openai/v1"
        )
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def run_bash(self, cmd: str) -> dict:
        """Executes a bash command in a sandboxed subprocess."""
        # Stripped env
        safe_env = os.environ.copy()
        if "AI_API_KEY" in safe_env:
            del safe_env["AI_API_KEY"]
        
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=self.repo_root,
                env=safe_env,
                preexec_fn=set_limits,
                timeout=30,
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
                "stderr": "Command timed out after 30 seconds"
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e)
            }
            
    def run_task(self, task: str):
        print(f"Starting task: {task}")
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task}
        ]
        
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run_bash",
                    "description": "Execute a bash command in the repository root.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The bash command to execute"
                            }
                        },
                        "required": ["command"]
                    }
                }
            }
        ]
        
        # Single execution turn for Phase 1/2 proof of concept
        response = self.client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=messages,
            tools=tools
        )
        
        message = response.choices[0].message
        if message.content:
            print("Model response:", message.content)
        
        # Process tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if tool_call.function.name == "run_bash":
                    args = json.loads(tool_call.function.arguments)
                    cmd = args["command"]
                    print(f"\nExecuting: {cmd}")
                    result = self.run_bash(cmd)
                    print(f"Exit Code: {result['exit_code']}")
                    if result['stdout']:
                        print(f"Stdout:\n{result['stdout']}")
                    if result['stderr']:
                        print(f"Stderr:\n{result['stderr']}")
