import os
import subprocess
import resource
import json
import time
from openai import OpenAI

SYSTEM_PROMPT = """You are a Proof-First AI Coding Harness agent.
You have exactly one tool available: `run_bash`.
You must navigate the repository yourself using `grep`, `find`, `cat`.
For modifying code or creating repository files, you MUST write a unified diff to `/tmp/patch.diff` AND apply it immediately using `patch` in the VERY SAME COMMAND.
Example:
cat << 'EOF' > /tmp/patch.diff
--- /dev/null
+++ b/hello.txt
@@ -0,0 +1 @@
+success
EOF
patch -p0 < /tmp/patch.diff

Never attempt to overwrite files inline or use custom editors.

After you have completed the requested changes, you MUST explicitly verify them using a bash command (for example, `cat hello.txt` to verify contents).
Once verified and successful, you MUST output the exact tag: <status>TASK_COMPLETE</status>
Your goal is to solve the given task using bash commands.
"""

def set_limits():
    try:
        mem_limit = 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        if hasattr(resource, 'RLIMIT_NPROC'):
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    except Exception:
        pass

class Orchestrator:
    def __init__(self):
        self.api_key = os.environ.get("AI_API_KEY")
        if not self.api_key:
            raise ValueError("AI_API_KEY environment variable is missing")
        self.base_url = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
        self.model = os.environ.get("AI_MODEL", "openai/gpt-oss-20b")
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.status = {
            "task": "",
            "iteration": 0,
            "commands_run": [],
            "rollback_count": 0,
            "state": "running",
            "tokens": 0
        }
        self.nudge_queue = []

    def _write_status(self):
        with open(os.path.join(self.repo_root, "status.json"), "w") as f:
            json.dump(self.status, f, indent=2)

    def checkpoint(self):
        # Create a commit snapshot so we don't wipe the working tree like stash does on success
        subprocess.run("git add -A && git -c user.email=harness@example.com -c user.name=Harness commit -m 'harness_checkpoint' || true", shell=True, cwd=self.repo_root, capture_output=True)

    def rollback(self):
        self.status["rollback_count"] += 1
        # Hard reset to the last checkpoint and clean untracked files
        subprocess.run("git reset --hard HEAD && git clean -fd", shell=True, cwd=self.repo_root, capture_output=True)
        self._write_status()

    def scrub_env(self):
        subprocess.run("rm -f /tmp/*.patch /tmp/*.diff", shell=True, cwd=self.repo_root)

    def distill_logs(self, exit_code, stdout, stderr):
        lines = (stdout + "\n" + stderr).splitlines()
        error_lines = []
        for line in lines:
            lower = line.lower()
            if "error" in lower or "exception" in lower or "traceback" in lower or "fail" in lower:
                error_lines.append(line)
        distilled = "\n".join(error_lines)
        if not distilled:
            distilled = stderr[-500:] if stderr else stdout[-500:]
        return f"Exit code {exit_code}\nDistilled output:\n{distilled}"

    def run_bash(self, cmd: str) -> dict:
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
            
    def pre_flight_critique(self, task: str) -> dict | None:
        messages = [
            {"role": "system", "content": "You are a senior software architect reviewing a coding task. Evaluate the task for viability, complexity, and better alternatives (e.g. using a standard library instead of custom logic). If you find a significantly better or cheaper approach, output a JSON object exactly like this: {'Original_Est_Tokens': 100, 'New_Est_Tokens': 50, 'Complexity': 'Low', 'Recommendation_Reason': 'reason', 'Recommended_Task': 'new task'}. If the original task is perfectly fine and requires no changes, output exactly the string 'PASS'."},
            {"role": "user", "content": f"Task: {task}"}
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages
            )
            if response.usage:
                self.status["tokens"] += response.usage.total_tokens
                self._write_status()
            content = response.choices[0].message.content or ""
            if "PASS" in content.upper() and "{" not in content:
                return None
            import re
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return None
        except Exception:
            return None

    def run_task(self, task: str):
        print(f"Starting task: {task}")
        
        critique = self.pre_flight_critique(task)
        if critique and hasattr(self, 'intercept_callback'):
            task = self.intercept_callback(critique, task)

        
        with open(os.path.join(self.repo_root, "TASK.md"), "w") as f:
            f.write(task)
            
        self.status["task"] = task
        self._write_status()
        
        style_path = os.path.join(self.repo_root, "agent_style.json")
        system_content = SYSTEM_PROMPT
        if os.path.exists(style_path):
            try:
                with open(style_path, "r") as f:
                    style_data = json.load(f)
                    system_content += "\n\nAdditional Coding Style and Context:\n"
                    system_content += json.dumps(style_data, indent=2)
            except Exception as e:
                print(f"Error parsing agent_style.json: {e}")
                
        self.messages = [
            {"role": "system", "content": system_content},
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
        
        iteration = 0
        max_iterations = 8
        is_retry = False
        outcome = "Failed"
        
        try:
            while iteration < max_iterations:
                while self.nudge_queue:
                    hint = self.nudge_queue.pop(0)
                    self.messages.append({"role": "user", "content": f"User nudge: {hint}"})
                    
                iteration += 1
                self.status["iteration"] = iteration
                self._write_status()
                print(f"\n--- Iteration {iteration} ---")
                
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    tools=tools
                )
                
                if response.usage:
                    self.status["tokens"] += response.usage.total_tokens
                    self._write_status()
                
                message = response.choices[0].message
                content = message.content or ""
                if content:
                    print("Model response:", content)
                    if "<status>TASK_COMPLETE</status>" in content:
                        print("Task completed successfully!")
                        commit = True
                        if hasattr(self, 'confirm_callback'):
                            commit = self.confirm_callback()
                            
                        if commit:
                            outcome = "Success"
                            self.status["state"] = "complete"
                        else:
                            outcome = "Failed"
                            self.status["state"] = "rolled_back"
                            self.rollback()
                        break
                
                if not message.tool_calls:
                    if "<status>TASK_COMPLETE</status>" not in content:
                        self.messages.append({"role": "assistant", "content": content})
                        self.messages.append({"role": "user", "content": "Please continue to verify and output <status>TASK_COMPLETE</status> when done, or use the run_bash tool."})
                    continue
                    
                tool_call = message.tool_calls[0]
                if tool_call.function.name == "run_bash":
                    args = json.loads(tool_call.function.arguments)
                    cmd = args.get("command", "")
                    
                    is_modifying = any(kw in cmd for kw in ["patch", ">", "rm ", "touch ", "sed "])
                    
                    if is_modifying:
                        self.checkpoint()
                    
                    self.scrub_env()
                    
                    print(f"Executing: {cmd}")
                    
                    cmd_log = {"iteration": iteration, "command": cmd, "timestamp": time.time()}
                    
                    result = self.run_bash(cmd)
                    exit_code = result["exit_code"]
                    
                    cmd_log["exit_code"] = exit_code
                    self.status["commands_run"].append(cmd_log)
                    self._write_status()
                    
                    print(f"Exit Code: {exit_code}")
                    if result['stdout']: print(f"Stdout:\n{result['stdout'][:500]}")
                    if result['stderr']: print(f"Stderr:\n{result['stderr'][:500]}")
                    
                    tool_call_dict = {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    }
                    self.messages.append({"role": "assistant", "content": content, "tool_calls": [tool_call_dict]})
                    
                    if exit_code == 0:
                        is_retry = False
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "run_bash",
                            "content": "Command succeeded.\n" + result["stdout"][-500:]
                        })
                    else:
                        is_retry = True
                        print("Command failed! Pruning context and rolling back...")
                        if is_modifying:
                            self.rollback()
                            
                        # Pruning context: pop the assistant message containing the tool call
                        if self.messages and self.messages[-1]["role"] == "assistant":
                            self.messages.pop()
                            
                        distilled_msg = self.distill_logs(exit_code, result["stdout"], result["stderr"])
                        self.messages.append({
                            "role": "user",
                            "content": f"Attempt failed when running `{cmd}`.\n{distilled_msg}\nPlease fix the error and try again."
                        })
            
            if iteration >= max_iterations and outcome == "Failed":
                print("\nReached max iterations (8). Exiting with partial status report.")
                self.status["state"] = "partial"
                
        finally:
            self._write_status()
            with open(os.path.join(self.repo_root, "OUTCOME.md"), "w") as f:
                f.write(f"# Run Outcome\n\n")
                f.write(f"**Status:** {self.status['state']}\n")
                f.write(f"**Iterations:** {iteration} / {max_iterations}\n")
                f.write(f"**Rollbacks:** {self.status['rollback_count']}\n\n")
                f.write("## Commands Run\n")
                for c in self.status["commands_run"]:
                    f.write(f"- Iteration {c['iteration']}: `{c['command']}` (Exit: {c.get('exit_code', 'N/A')})\n")
