import os
import json
import time
import subprocess
import logging
from config import Config
from llm_client import LLMClient
from sandbox import run_sandboxed
from evidence import Evidence

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

class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.llm = LLMClient(config)
        self.evidence = Evidence(self.repo_root)
        self.nudge_queue = []

    def checkpoint(self):
        subprocess.run("git add -A && git -c user.email=harness@example.com -c user.name=Harness commit -m 'harness_checkpoint' || true", shell=True, cwd=self.repo_root, capture_output=True)

    def rollback(self):
        self.evidence.status["rollback_count"] += 1
        subprocess.run("git reset --hard HEAD && git clean -fd", shell=True, cwd=self.repo_root, capture_output=True)
        self.evidence.write_status()

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

    @property
    def status(self):
        self.evidence.status["tokens"] = self.llm.usage_tokens
        return self.evidence.status

    def run_task(self, task: str):
        logging.info(f"Starting task: {task}")
        
        critique = self.llm.critique(task)
        if critique and hasattr(self, 'intercept_callback'):
            task = self.intercept_callback(critique, task)

        self.evidence.write_task(task)
        
        style_path = os.path.join(self.repo_root, "agent_style.json")
        system_content = SYSTEM_PROMPT
        if os.path.exists(style_path):
            try:
                with open(style_path, "r") as f:
                    style_data = json.load(f)
                    system_content += "\n\nAdditional Coding Style and Context:\n"
                    system_content += json.dumps(style_data, indent=2)
            except Exception as e:
                logging.info(f"Error parsing agent_style.json: {e}")
                
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
        outcome = "Failed"
        
        try:
            while iteration < self.config.iteration_cap:
                while self.nudge_queue:
                    hint = self.nudge_queue.pop(0)
                    self.messages.append({"role": "user", "content": f"User nudge: {hint}"})
                    
                iteration += 1
                self.evidence.status["iteration"] = iteration
                self.evidence.write_status()
                logging.info(f"\n--- Iteration {iteration} ---")
                
                message = self.llm.step(self.messages, tools)
                self.evidence.status["tokens"] = self.llm.usage_tokens
                self.evidence.write_status()
                
                content = message.content or ""
                if content:
                    logging.info(f"Model response: {content}")
                    if "<status>TASK_COMPLETE</status>" in content:
                        logging.info("Task completed successfully!")
                        commit = True
                        if hasattr(self, 'confirm_callback'):
                            commit = self.confirm_callback()
                            
                        if commit:
                            outcome = "Success"
                            self.evidence.status["state"] = "complete"
                        else:
                            outcome = "Failed"
                            self.evidence.status["state"] = "rolled_back"
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
                    
                    logging.info(f"Executing: {cmd}")
                    
                    cmd_log = {"iteration": iteration, "command": cmd, "timestamp": time.time()}
                    
                    result = run_sandboxed(cmd, self.repo_root, self.config.timeout_seconds, self.config.mem_limit_mb)
                    exit_code = result["exit_code"]
                    
                    cmd_log["exit_code"] = exit_code
                    self.evidence.status["commands_run"].append(cmd_log)
                    self.evidence.write_status()
                    
                    logging.info(f"Exit Code: {exit_code}")
                    if result['stdout']: logging.info(f"Stdout:\n{result['stdout'][:500]}")
                    if result['stderr']: logging.info(f"Stderr:\n{result['stderr'][:500]}")
                    
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
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "run_bash",
                            "content": "Command succeeded.\n" + result["stdout"][-500:]
                        })
                    else:
                        logging.info("Command failed! Pruning context and rolling back...")
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
            
            if iteration >= self.config.iteration_cap and outcome == "Failed":
                logging.info("\nReached max iterations. Exiting with partial status report.")
                self.evidence.status["state"] = "partial"
                
        finally:
            self.evidence.write_outcome(self.config.iteration_cap)
