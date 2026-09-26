import os
import json
import time
import subprocess
import logging
from config import Config
from llm_client import LLMClient
from sandbox import run_sandboxed
from evidence import Evidence
from exceptions import HarnessError, PatchApplyError, RollbackError
from context_manager import ContextManager

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
        try:
            subprocess.run("git add -A && git -c user.email=harness@example.com -c user.name=Harness commit -m 'harness_checkpoint' || true", shell=True, cwd=self.repo_root, capture_output=True, check=True)
        except Exception as e:
            logging.error(f"Checkpoint failed: {e}")
            raise PatchApplyError(f"Checkpoint failed: {e}") from e

    def rollback(self):
        self.evidence.status["rollback_count"] += 1
        try:
            subprocess.run("git reset --hard HEAD && git clean -fd", shell=True, cwd=self.repo_root, capture_output=True, check=True)
        except Exception as e:
            logging.error(f"Rollback failed: {e}")
            raise RollbackError(f"Rollback failed: {e}") from e
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
                
        self.context = ContextManager([
            {"role": "system", "content": system_content},
            {"role": "user", "content": task}
        ])
        
        tools = [{"type": "function", "function": {"name": "run_bash", "description": "Execute a bash command in the repository root.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to execute"}}, "required": ["command"]}}}]
        
        iteration = 0
        outcome = "Failed"
        is_retry = False
        
        try:
            while iteration < self.config.iteration_cap:
                while self.nudge_queue:
                    hint = self.nudge_queue.pop(0)
                    self.context.append({"role": "user", "content": f"User nudge: {hint}"})
                    
                iteration += 1
                self.evidence.status["iteration"] = iteration
                self.evidence.write_status()
                logging.info(f"\n--- Iteration {iteration} ---")
                
                try:
                    message = self.llm.step(self.context.get_messages(), tools)
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
                            self.context.append({"role": "assistant", "content": content})
                            self.context.append({"role": "user", "content": "Please continue to verify and output <status>TASK_COMPLETE</status> when done, or use the run_bash tool."})
                        continue
                        
                    tool_call = message.tool_calls[0]
                    if tool_call.function.name == "run_bash":
                        args = json.loads(tool_call.function.arguments)
                        cmd = args.get("command", "")
                        
                        tool_call_dict = {"id": tool_call.id, "type": "function", "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments}}
                        self.context.append({"role": "assistant", "content": content, "tool_calls": [tool_call_dict]})
                        
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
                        
                        critique_failed = False
                        if exit_code == 0:
                            if is_modifying:
                                diff_output = subprocess.run("git diff", shell=True, cwd=self.repo_root, capture_output=True, text=True).stdout
                                changed_files = [line for line in diff_output.splitlines() if line.startswith("diff --git")]
                                num_files = len(changed_files)
                                changed_lines = len([line for line in diff_output.splitlines() if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))])
                                
                                if num_files > 1 or changed_lines > 15 or is_retry:
                                    logging.info("Triggering post-patch self-critique...")
                                    critique_result = self.llm.post_patch_critique(task, diff_output)
                                    if "critique_events" not in self.evidence.status:
                                        self.evidence.status["critique_events"] = []
                                    decision = critique_result.get("decision", "fail").lower()
                                    reason = critique_result.get("reason", "No reason provided")
                                    self.evidence.status["critique_events"].append({"iteration": iteration, "action": "fire", "decision": decision, "reason": reason})
                                    self.evidence.write_status()
                                    if decision == "fail":
                                        critique_failed = True
                                        logging.info("Critique failed! Rolling back.")
                                        self.rollback()
                                        failure_summary = f"Attempt applied successfully but self-critique rejected the changes:\n{reason}\nPlease roll back your mental state and try a different approach."
                                        self.context.prune_and_note(failure_summary)
                                else:
                                    if "critique_events" not in self.evidence.status:
                                        self.evidence.status["critique_events"] = []
                                    self.evidence.status["critique_events"].append({"iteration": iteration, "action": "skip", "reason": f"num_files={num_files}, changed_lines={changed_lines}, is_retry={is_retry}"})
                                    self.evidence.write_status()
                            
                            if not critique_failed:
                                is_retry = False
                                self.context.append({"role": "tool", "tool_call_id": tool_call.id, "name": "run_bash", "content": "Command succeeded.\n" + result["stdout"][-500:]})
                            else:
                                is_retry = True
                        else:
                            is_retry = True
                            logging.info("Command failed! Pruning context and rolling back...")
                            if is_modifying:
                                self.rollback()
                            distilled_msg = self.distill_logs(exit_code, result["stdout"], result["stderr"])
                            failure_summary = f"Attempt failed when running `{cmd}`.\n{distilled_msg}\nPlease fix the error and try again."
                            self.context.prune_and_note(failure_summary)
                            
                except HarnessError as e:
                    logging.error(f"Harness error encountered: {e}")
                    is_retry = True
                    try:
                        self.rollback()
                    except Exception as rb_e:
                        logging.error(f"Fatal rollback error after harness error: {rb_e}")
                    failure_summary = f"Internal system error occurred:\n{e}\nPlease try an alternative approach or fix the error."
                    self.context.prune_and_note(failure_summary)
            if iteration >= self.config.iteration_cap and outcome == "Failed":
                logging.info("\nReached max iterations. Exiting with partial status report.")
                self.evidence.status["state"] = "partial"
        finally:
            self.evidence.write_outcome(self.config.iteration_cap)
