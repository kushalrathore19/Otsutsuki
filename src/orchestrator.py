import os
import json
import time
import subprocess
import logging
from config import Config
from llm_client import LLMClient
from sandbox import run_sandboxed
from evidence import Evidence
from audit import AuditLog
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
        self.audit = AuditLog(self.repo_root)
        self.nudge_queue = []

    def reset(self):
        self.evidence.archive()
        self.evidence = Evidence(self.repo_root)
        self.nudge_queue = []
        
    def _run_reviewer(self, diff_output: str):
        prompt = """You are a Code Reviewer. Review the following diff and provide ONLY non-blocking suggestions for improvement (code smell, missing docstrings, naming conventions, etc.). Do NOT reject the code, just provide plain text notes. If there is nothing to note, reply with 'No notes.'"""
        try:
            response = self.llm.step([{"role": "system", "content": prompt}, {"role": "user", "content": f"Diff:\n{diff_output}"}], [])
            notes = response.content or "No notes."
            with self.evidence.lock:
                self.evidence.status["reviewer_notes"] = notes
            self.evidence.write_status()
        except Exception as e:
            logging.warning(f"Reviewer failed: {e}")

    def _create_pr(self, task: str):
        if os.environ.get("CREATE_PR") == "1":
            try:
                auth_check = subprocess.run("gh auth status", shell=True, cwd=self.repo_root, capture_output=True)
                if auth_check.returncode == 0:
                    with open(os.path.join(self.repo_root, "OUTCOME.md"), "r") as f:
                        body = f.read()
                    import tempfile
                    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                        tf.write(body)
                        tf_name = tf.name
                    subprocess.run(f"gh pr create --title 'AI Task Complete' --body-file {tf_name}", shell=True, cwd=self.repo_root, capture_output=True)
                    os.unlink(tf_name)
            except Exception:
                pass

    def checkpoint(self, cwd=None):
        cwd = cwd or self.repo_root
        self.audit.log_event("checkpoint", {"cwd": cwd})
        try:
            subprocess.run("git add -A && git -c user.email=harness@example.com -c user.name=Harness commit -m 'harness_checkpoint' || true", shell=True, cwd=cwd, capture_output=True, check=True)
        except Exception as e:
            logging.error(f"Checkpoint failed: {e}")
            raise PatchApplyError(f"Checkpoint failed: {e}") from e

    def rollback(self, cwd=None):
        cwd = cwd or self.repo_root
        self.audit.log_event("rollback", {"cwd": cwd})
        self.evidence.status["rollback_count"] += 1
        try:
            subprocess.run("git reset --hard HEAD && git clean -fd", shell=True, cwd=cwd, capture_output=True, check=True)
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
        
    def _execute_subtask(self, task: str, iteration: int = 0, bypass_confirm: bool = False, cwd: str = None):
        cwd = cwd or self.repo_root
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
        
        outcome = "Failed"
        is_retry = False
        surgical_injected = False
        finalize_injected = False
        
        while iteration < self.config.iteration_cap:
            while self.nudge_queue:
                hint = self.nudge_queue.pop(0)
                self.context.append({"role": "user", "content": f"User nudge: {hint}"})
                
            iteration += 1
            self.evidence.status["iteration"] = iteration
            self.evidence.write_status()
            logging.info(f"\n--- Iteration {iteration} ---")
            
            try:
                if self.config.token_budget > 0:
                    budget_fraction = self.llm.usage_tokens / self.config.token_budget
                    if budget_fraction >= self.config.finalize_fraction and not finalize_injected:
                        self.context.append({"role": "user", "content": "Token budget nearly exhausted. Finalize your current approach now and verify — do not start anything new."})
                        finalize_injected = True
                        self.evidence.status["mode"] = "finalize"
                        logging.info("Injected FINALIZE budget mode.")
                    elif budget_fraction >= self.config.surgical_fraction and not surgical_injected:
                        self.context.append({"role": "user", "content": f"Token budget is {int(budget_fraction * 100)}% used. Be surgical — avoid further exploration, make the smallest change that satisfies the task."})
                        surgical_injected = True
                        self.evidence.status["mode"] = "surgical"
                        logging.info("Injected SURGICAL budget mode.")

                message = self.llm.step(self.context.get_messages(), tools)
                self.evidence.status["tokens"] = self.llm.usage_tokens
                self.evidence.write_status()
                
                content = message.content or ""
                if content:
                    logging.info(f"Model response: {content}")
                    if "<status>TASK_COMPLETE</status>" in content:
                        logging.info("Task completed successfully!")
                        if bypass_confirm:
                            outcome = "Success"
                        else:
                            commit = True
                            if hasattr(self, 'confirm_callback'):
                                commit = self.confirm_callback()
                            self.audit.log_event("confirm_gate", {"approved": commit})
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
                        self.checkpoint(cwd=cwd)
                    self.scrub_env()
                    
                    logging.info(f"Executing: {cmd}")
                    cmd_log = {"iteration": iteration, "command": cmd, "timestamp": time.time()}
                    result = run_sandboxed(cmd, cwd, self.config.timeout_seconds, self.config.mem_limit_mb)
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
                            diff_output = subprocess.run("git diff", shell=True, cwd=cwd, capture_output=True, text=True).stdout
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
                                self.audit.log_event("critique_fired", {"decision": decision})
                                self.evidence.status["critique_events"].append({"iteration": iteration, "action": "fire", "decision": decision, "reason": reason})
                                self.evidence.write_status()
                                if decision == "fail":
                                    critique_failed = True
                                    logging.info("Critique failed! Rolling back.")
                                    self.rollback(cwd=cwd)
                                    failure_summary = f"Attempt applied successfully but self-critique rejected the changes:\n{reason}\nPlease roll back your mental state and try a different approach."
                                    self.context.prune_and_note(failure_summary)
                            else:
                                self.audit.log_event("critique_skipped")
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
                            self.rollback(cwd=cwd)
                        distilled_msg = self.distill_logs(exit_code, result["stdout"], result["stderr"])
                        failure_summary = f"Attempt failed when running `{cmd}`.\n{distilled_msg}\nPlease fix the error and try again."
                        self.context.prune_and_note(failure_summary)
                        
            except HarnessError as e:
                logging.error(f"Harness error encountered: {e}")
                is_retry = True
                try:
                    self.rollback(cwd=cwd)
                except Exception as rb_e:
                    logging.error(f"Fatal rollback error after harness error: {rb_e}")
                failure_summary = f"Internal system error occurred:\n{e}\nPlease try an alternative approach or fix the error."
                self.context.prune_and_note(failure_summary)
                
        return outcome, iteration

    def run_task(self, task: str):
        self.audit.log_event("task_started", {"task": task})
        logging.info(f"Starting task: {task}")
        critique = self.llm.critique(task)
        if critique and hasattr(self, 'intercept_callback'):
            task = self.intercept_callback(critique, task)
        self.evidence.write_task(task)
        
        try:
            outcome, iteration = self._execute_subtask(task, iteration=0, bypass_confirm=False)
            if iteration >= self.config.iteration_cap and outcome == "Failed":
                logging.info("\nReached max iterations. Exiting with partial status report.")
                self.evidence.status["state"] = "partial"
                
            if self.evidence.status.get("state") == "complete":
                diff_output = subprocess.run("git diff HEAD~1 HEAD", shell=True, cwd=self.repo_root, capture_output=True, text=True).stdout
                self._run_reviewer(diff_output)
                self.evidence.write_outcome(self.config.iteration_cap)
                self._create_pr(task)
                
            self.audit.log_event("run_completed", {"state": self.evidence.status.get("state")})
        finally:
            self.evidence.write_outcome(self.config.iteration_cap)

    def run_multi_agent(self, task: str):
        self.audit.log_event("task_started", {"task": task})
        from architect import Architect
        from verifier import Verifier
        
        logging.info(f"Starting multi-agent task: {task}")
        critique = self.llm.critique(task)
        if critique and hasattr(self, 'intercept_callback'):
            task = self.intercept_callback(critique, task)
        self.evidence.write_task(task)
        
        pre_run_commit = subprocess.run("git rev-parse HEAD", shell=True, cwd=self.repo_root, capture_output=True, text=True).stdout.strip()
        if not pre_run_commit:
            pre_run_commit = "HEAD"
            
        if hasattr(self, 'role_callback'):
            self.role_callback("Architect")
            
        architect = Architect(self.llm)
        plan = architect.plan(task)
        
        for s in plan:
            if "depends_on" not in s: s["depends_on"] = []
            if "expected_files" not in s: s["expected_files"] = []
            
        def save_plan():
            with open(os.path.join(self.repo_root, "PLAN.md"), "w") as f:
                f.write(json.dumps(plan, indent=2))
        save_plan()
            
        iteration = 0
        verifier = Verifier(self.llm)
        import concurrent.futures
        
        try:
            while iteration < self.config.iteration_cap:
                pending_subtasks = [s for s in plan if s.get("status", "pending") in ["pending", "failed", "conflict"]]
                if not pending_subtasks:
                    break
                    
                ready_subtasks = []
                for s in pending_subtasks:
                    deps = s.get("depends_on", [])
                    deps_done = all(any(d.get("id") == dep_id and d.get("status") in ["done", "success"] for d in plan) for dep_id in deps)
                    if deps_done:
                        ready_subtasks.append(s)
                
                if not ready_subtasks:
                    break
                    
                groups = []
                for rs in ready_subtasks:
                    if rs.get("status") == "conflict":
                        groups.append([rs])
                        continue
                    placed = False
                    for g in groups:
                        overlap = False
                        for existing_rs in g:
                            if set(rs.get("expected_files", [])).intersection(set(existing_rs.get("expected_files", []))):
                                overlap = True
                                break
                        if not overlap:
                            g.append(rs)
                            placed = True
                            break
                    if not placed:
                        groups.append([rs])
                        
                group = groups[0]
                
                if hasattr(self, 'role_callback'):
                    self.role_callback("Implementer")
                    
                use_wts = []
                fallback_sequential = False
                if len(group) > 1:
                    for subtask in group:
                        wt_path = os.path.abspath(os.path.join(self.repo_root, f"../otsutsuki-wt-{subtask['id']}"))
                        wt_branch = f"agent/subtask-{subtask['id']}"
                        try:
                            subprocess.run(f"git worktree add {wt_path} -b {wt_branch} HEAD", shell=True, cwd=self.repo_root, check=True, capture_output=True)
                            use_wts.append((subtask, wt_path, wt_branch))
                        except Exception as e:
                            logging.warning(f"Worktree failed for {subtask['id']}: {e}. Falling back to sequential for group.")
                            fallback_sequential = True
                            break
                            
                if fallback_sequential or len(group) == 1:
                    for s, wp, wb in use_wts:
                        subprocess.run(f"git worktree remove {wp} --force", shell=True, cwd=self.repo_root, capture_output=True)
                        subprocess.run(f"git branch -D {wb}", shell=True, cwd=self.repo_root, capture_output=True)
                    
                    for subtask in group:
                        self.audit.log_event("subtask_started", {"subtask_id": subtask["id"]})
                        subtask["status"] = "running"
                        save_plan()
                        outcome, iteration = self._execute_subtask(subtask["description"], iteration=iteration, bypass_confirm=True, cwd=self.repo_root)
                        self.audit.log_event("subtask_completed", {"subtask_id": subtask["id"], "outcome": outcome})
                        subtask["status"] = "done" if outcome == "Success" else "failed"
                        save_plan()
                else:
                    for subtask in group:
                        self.audit.log_event("subtask_started", {"subtask_id": subtask["id"]})
                        subtask["status"] = "running"
                    save_plan()
                    
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(group)) as executor:
                        futures = {}
                        for subtask, wt_path, wt_branch in use_wts:
                            future = executor.submit(self._execute_subtask, subtask["description"], iteration, True, wt_path)
                            futures[future] = (subtask, wt_path, wt_branch)
                            
                        for future in concurrent.futures.as_completed(futures):
                            subtask, wt_path, wt_branch = futures[future]
                            try:
                                outcome, new_iter = future.result()
                                iteration = max(iteration, new_iter)
                                self.audit.log_event("subtask_completed", {"subtask_id": subtask["id"], "outcome": outcome})
                                subtask["status"] = "done" if outcome == "Success" else "failed"
                            except Exception as e:
                                logging.error(f"Subtask failed with exception: {e}")
                                subtask["status"] = "failed"
                                
                            if subtask["status"] == "done":
                                try:
                                    subprocess.run(f"git merge {wt_branch}", shell=True, cwd=self.repo_root, check=True, capture_output=True)
                                except Exception as e:
                                    logging.warning(f"Merge conflict for {wt_branch}. Aborting merge and requeuing sequentially.")
                                    subprocess.run(f"git merge --abort", shell=True, cwd=self.repo_root, capture_output=True)
                                    subtask["status"] = "conflict"
                            
                            subprocess.run(f"git worktree remove {wt_path} --force", shell=True, cwd=self.repo_root, capture_output=True)
                            subprocess.run(f"git branch -D {wt_branch}", shell=True, cwd=self.repo_root, capture_output=True)
                            save_plan()
                            
                if any(s.get("status") not in ["done", "success"] for s in plan):
                    break
                    
                if hasattr(self, 'role_callback'):
                    self.role_callback("Verifier")
                    
                diff_output = subprocess.run(f"git diff {pre_run_commit}", shell=True, cwd=self.repo_root, capture_output=True, text=True).stdout
                v_res = verifier.verify(task, diff_output)
                
                if v_res.get("decision") == "pass":
                    self.audit.log_event("verifier_pass")
                    commit = True
                    if hasattr(self, 'confirm_callback'):
                        commit = self.confirm_callback()
                    self.audit.log_event("confirm_gate", {"approved": commit})
                    if commit:
                        self.evidence.status["state"] = "complete"
                        self._run_reviewer(diff_output)
                        self.evidence.write_outcome(self.config.iteration_cap)
                        self._create_pr(task)
                    else:
                        self.evidence.status["state"] = "rolled_back"
                        self.rollback()
                    break
                else:
                    failed_id = v_res.get("failed_subtask_id")
                    self.audit.log_event("verifier_fail", {"failed_subtask_id": failed_id})
                    reason = v_res.get("reason", "No reason provided")
                    logging.info(f"Verifier failed. Reason: {reason}")
                    
                    if failed_id is not None:
                        target = next((s for s in plan if s["id"] == failed_id), None)
                        if target:
                            retries = target.get("retries", 0)
                            if retries >= 2:
                                logging.info("Max retries for subtask reached. Degrading.")
                                self.evidence.status["state"] = "partial"
                                break
                            target["retries"] = retries + 1
                            target["status"] = "failed"
                            target["description"] += f"\n\nVERIFIER FEEDBACK to fix: {reason}"
                        else:
                            self.evidence.status["state"] = "partial"
                            break
                    else:
                        self.evidence.status["state"] = "partial"
                        break
                        
            if self.evidence.status.get("state") not in ["complete", "rolled_back"]:
                self.evidence.status["state"] = "partial"
            self.audit.log_event("run_completed", {"state": self.evidence.status.get("state")})
        finally:
            self.evidence.write_outcome(self.config.iteration_cap)
