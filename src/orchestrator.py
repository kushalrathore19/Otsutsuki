import os
import json
import sys
import time
import subprocess
import logging
from config import Config
from llm_client import LLMClient
from sandbox import run_sandboxed
from patcher import apply_patch_diff
from safe_reader import safe_read_file
from evidence import Evidence
from audit import AuditLog
from exceptions import HarnessError, PatchApplyError, RollbackError
from context_manager import ContextManager

SYSTEM_PROMPT = """You are an autonomous coding agent working inside a git repository. You have three tools.

1. `read_file(path, start_line, end_line)` — range-based file reader that prints line numbers. ALWAYS use this instead of cat/head/tail: it rejects minified/binary files, caps output size, and its header reports total_lines. Read in windows (e.g. 1-200) and widen or advance as needed.
2. `apply_patch(diff)` — applies a unified diff with fuzzy matching: it tolerates whitespace drift and stale line numbers, creates/deletes files, and is idempotent on re-apply. This is the ONLY way to edit files. If it returns 'PATCH_ERROR: ...', re-read the affected range with read_file and regenerate the hunk with exact context lines.
3. `run_bash(command)` — sandboxed bash (confined to the repository, hard wall-clock timeout, destructive commands blocked). Use it for builds, tests, grep, and git inspection. Never use shell redirects to edit files.

Workflow: read_file the relevant range -> apply_patch with a unified diff (copy context lines exactly from read_file output) -> run_bash to run tests and verify. Keep diffs minimal: one hunk per logical change.
When the task is verified complete, output: <status>TASK_COMPLETE</status>"""

class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.repo_root = os.path.abspath(config.target_repo)
        
        if not os.path.isdir(self.repo_root):
            logging.error(f"Target repo {self.repo_root} does not exist.")
            import sys; sys.exit(1)
        if not os.path.isdir(os.path.join(self.repo_root, ".git")):
            logging.error(f"Target repo {self.repo_root} is not a git repository.")
            import sys; sys.exit(1)
            
        self.llm = LLMClient(config)
        self.evidence = Evidence(self.repo_root)
        self.audit = AuditLog(self.repo_root)
        self.nudge_queue = []

    def reset(self):
        self.evidence.archive()
        self.evidence = Evidence(self.repo_root)
        self.nudge_queue = []
        
    def localize(self, task: str) -> str:
        import re
        import ast
        terms = set()
        for m in re.findall(r'"([^"]+)"|\'([^\']+)\'', task):
            terms.add(m[0] or m[1])
        terms.update(re.findall(r'\b[\w\./\-]+\.\w+\b', task))
        terms.update(re.findall(r'\b[a-z]+(?:_[a-z0-9]+)+\b', task))
        terms.update(re.findall(r'\b[A-Z][a-zA-Z0-9]+\b', task))
        
        candidates = {}
        for term in terms:
            if len(term) < 4: continue
            try:
                res = subprocess.run(
                    ["grep", "-rn", "--exclude-dir=.git", "--exclude-dir=__pycache__", "--exclude-dir=node_modules", term, self.repo_root],
                    capture_output=True, text=True, timeout=5
                )
                for line in res.stdout.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fp, lineno = parts[0], int(parts[1])
                        if not os.path.exists(fp) or not os.path.isfile(fp): continue
                        candidates.setdefault(fp, []).append((lineno, term))
            except Exception:
                pass
                
        if not candidates:
            return "No existing code matched — this appears to require new file(s)."
            
        results = []
        for fp, matches in sorted(candidates.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
            symbol = "Unknown"
            matches = sorted(matches, key=lambda x: x[0])
            first_ln = matches[0][0]
            line_range = f"Line {first_ln}"
            if fp.endswith('.py'):
                try:
                    with open(fp, "r") as f:
                        source = f.read()
                    tree = ast.parse(source)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                            if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                                for lineno, _ in matches:
                                    if node.lineno <= lineno <= node.end_lineno:
                                        symbol = node.name
                                        line_range = f"Lines {node.lineno}-{node.end_lineno}"
                                        break
                except Exception:
                    pass
            rel_fp = os.path.relpath(fp, self.repo_root)
            snippet_lines = []
            try:
                with open(fp, "r") as f:
                    lines = f.readlines()
                start = max(0, first_ln - 2)
                end = min(len(lines), first_ln + 2)
                snippet_lines = [l.rstrip() for l in lines[start:end]]
            except:
                pass
            snippet_text = "\n  ".join(snippet_lines)
            results.append(f"- {rel_fp}: {symbol} ({line_range})\n  Snippet:\n  {snippet_text}")
            
        return "Likely relevant locations based on static analysis:\n" + "\n".join(results) + "\nVerify these yourself before assuming they're correct — this is a starting point, not ground truth."
        
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

    def _post_patch_gate(self, task: str, cwd: str, iteration: int, is_retry: bool, handle_failure) -> bool:
        """Deterministic post-edit checks shared by run_bash patches and apply_patch.

        Records diff stats, runs py_compile over changed Python files, then
        fires the conditional self-critique.  On failure it rolls the tree
        back and reports through ``handle_failure``/prune itself.

        Returns True when the working tree is acceptable.
        """
        diff_output = subprocess.run("git diff", shell=True, cwd=cwd, capture_output=True, text=True).stdout
        changed_files = [line.split(" b/")[-1] for line in diff_output.splitlines() if line.startswith("diff --git")]
        num_files = len(changed_files)
        changed_lines = len([line for line in diff_output.splitlines() if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))])
        self.evidence.status["files_changed"] = self.evidence.status.get("files_changed", 0) + num_files
        self.evidence.write_status()

        for cf in changed_files:
            if cf.endswith(".py") and os.path.exists(os.path.join(cwd, cf)):
                res = subprocess.run(f'"{sys.executable}" -m py_compile "{cf}"', shell=True, cwd=cwd, capture_output=True, text=True)
                if res.returncode != 0:
                    logging.info(f"Syntax pre-check failed on {cf}")
                    self.rollback(cwd=cwd)
                    failure_summary = f"Attempt applied successfully but introduced a SyntaxError in {cf}:\n{res.stderr}"
                    handle_failure(failure_summary, f"SyntaxError in {cf}")
                    return False

        if "critique_events" not in self.evidence.status:
            self.evidence.status["critique_events"] = []

        if num_files > 1 or changed_lines > 15 or is_retry:
            logging.info("Triggering post-patch self-critique...")
            critique_result = self.llm.post_patch_critique(task, diff_output)
            # The critique JSON may come back malformed; coerce before use
            # so a bad value degrades to a skipped critique, not a crash.
            decision = str(critique_result.get("decision", "fail")).lower()
            reason = str(critique_result.get("reason", "No reason provided"))
            self.audit.log_event("critique_fired", {"decision": decision})
            self.evidence.status["critique_events"].append({"iteration": iteration, "action": "fire", "decision": decision, "reason": reason})
            self.evidence.write_status()
            if decision == "fail":
                logging.info("Critique failed! Rolling back.")
                self.rollback(cwd=cwd)
                failure_summary = f"Attempt applied successfully but self-critique rejected the changes:\n{reason}\nPlease roll back your mental state and try a different approach."
                self.context.prune_and_note(failure_summary)
                return False
        else:
            self.audit.log_event("critique_skipped")
            self.evidence.status["critique_events"].append({"iteration": iteration, "action": "skip", "reason": f"num_files={num_files}, changed_lines={changed_lines}, is_retry={is_retry}"})
            self.evidence.write_status()
        return True

    @property
    def status(self):
        self.evidence.status["tokens"] = self.llm.usage_tokens
        return self.evidence.status

    def enrich_task_with_github(self, task: str) -> str:
        import re
        import urllib.request
        import json
        
        match = re.search(r'https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)', task)
        if not match:
            return task
            
        owner, repo, issue_num = match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}"
        
        try:
            req = urllib.request.Request(api_url)
            if "GITHUB_TOKEN" in os.environ:
                req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
            req.add_header("User-Agent", "Otsutsuki-Harness")
            
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode())
                    title = data.get("title", "")
                    body = data.get("body", "")
                    
                    enrichment = f"\n\n--- GitHub Issue #{issue_num}: {title} ---\n{body}\n------------------------\n"
                    
                    comments_url = data.get("comments_url")
                    if comments_url:
                        try:
                            creq = urllib.request.Request(comments_url)
                            if "GITHUB_TOKEN" in os.environ:
                                creq.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
                            creq.add_header("User-Agent", "Otsutsuki-Harness")
                            with urllib.request.urlopen(creq, timeout=10) as cresp:
                                if cresp.status == 200:
                                    cdata = json.loads(cresp.read().decode())
                                    if cdata:
                                        enrichment += "Comments:\n"
                                        for c in cdata:
                                            enrichment += f"- {c.get('user', {}).get('login', 'User')}: {c.get('body', '')}\n"
                        except Exception as ce:
                            logging.warning(f"Failed to fetch comments for issue #{issue_num}: {ce}")
                            
                    return task + enrichment
        except Exception as e:
            msg = f"Degradation: Failed to fetch GitHub issue #{issue_num}: {e}"
            logging.warning(msg)
            if "degradations" not in self.evidence.status:
                self.evidence.status["degradations"] = []
            self.evidence.status["degradations"].append(msg)
            self.evidence.write_status()
            
        return task


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
                
        localization_summary = self.localize(task)
        self.context = ContextManager([
            {"role": "system", "content": system_content},
            {"role": "user", "content": localization_summary + "\n\nTask:\n" + task}
        ])
        
        tools = [
            {"type": "function", "function": {
                "name": "run_bash",
                "description": "Execute a bash command in the repository root (sandboxed: repo-confined, timeout-enforced, destructive commands blocked).",
                "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to execute"}}, "required": ["command"]},
            }},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "Safely read a range of lines from a file, with line numbers. Rejects minified/binary files and enforces output limits. Use this instead of cat.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"},
                    "start_line": {"type": "integer", "description": "1-based first line to read (default 1)"},
                    "end_line": {"type": "integer", "description": "1-based inclusive last line (at most 400 lines per call)"},
                }, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "apply_patch",
                "description": "Apply a unified diff to the repository with fuzzy matching (tolerates whitespace drift and stale line numbers; creates and deletes files). Use this for ALL file edits.",
                "parameters": {"type": "object", "properties": {
                    "diff": {"type": "string", "description": "Unified diff text with ---/+++ file headers and @@ hunk headers"},
                }, "required": ["diff"]},
            }},
        ]
        
        outcome = "Failed"
        is_retry = False
        surgical_injected = False
        finalize_injected = False
        self.same_failure_count = 0
        
        def handle_failure(failure_summary, cmd_context):
            self.same_failure_count += 1
            if self.same_failure_count == 1:
                guidance = "Please fix the error and try again."
            elif self.same_failure_count == 2:
                guidance = "This is your second failed attempt at this specific issue. Do NOT repeat the same fix with minor variations — identify a fundamentally different approach to solve this."
            else:
                critique_prompt = f"The current approach has failed {self.same_failure_count} times: {cmd_context}. Is the overall approach wrong? Suggest a different strategy or confirm the approach is sound but execution needs to be more careful."
                critique_resp = self.llm.step([{"role": "user", "content": critique_prompt}], [])
                guidance = f"This approach has repeatedly failed. Analysis:\n{critique_resp.content}\nAdjust your strategy accordingly."
            
            full_summary = f"{failure_summary}\n{guidance}"
            
            if self.same_failure_count >= 3:
                self.context.messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": localization_summary + "\n\nTask:\n" + task},
                    {"role": "user", "content": f"Prior attempts consolidated summary:\n{full_summary}"}
                ]
            else:
                self.context.prune_and_note(full_summary)
        
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
                    
                tool_calls = message.tool_calls
                tool_call_dicts = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
                self.context.append({"role": "assistant", "content": content, "tool_calls": tool_call_dicts})
                self.evidence.status["tools_called"] = self.evidence.status.get("tools_called", 0) + len(tool_calls)
                # The protocol requires a response for every tool_call id;
                # this loop executes one call per turn and skips the rest.
                for extra in tool_calls[1:]:
                    self.context.append({
                        "role": "tool", "tool_call_id": extra.id, "name": extra.function.name,
                        "content": "SKIPPED: process one tool call per turn. Re-issue this call in your next message.",
                    })

                tool_call = tool_calls[0]
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError as json_err:
                    args = None
                    self.context.append({
                        "role": "tool", "tool_call_id": tool_call.id, "name": name,
                        "content": f"TOOL_ERROR: arguments were not valid JSON ({json_err}). Resend the call with valid JSON.",
                    })

                if name == "run_bash" and args is not None:
                    cmd = args.get("command", "")

                    is_modifying = any(kw in cmd for kw in ["patch", ">", "rm ", "touch ", "sed "])
                    if is_modifying:
                        self.checkpoint(cwd=cwd)
                    self.scrub_env()
                    
                    logging.info(f"Executing: {cmd}")
                    cmd_log = {"iteration": iteration, "command": cmd, "timestamp": time.time()}
                    result = run_sandboxed(cmd, cwd, self.config.timeout_seconds, self.config.mem_limit_mb, self.repo_root)
                    exit_code = result["exit_code"]
                    
                    cmd_log["exit_code"] = exit_code
                    
                    if "pytest" in cmd or "make test" in cmd or "npm test" in cmd:
                        self.evidence.status["tests_run"] = self.evidence.status.get("tests_run", 0) + 1
                        if exit_code == 0:
                            self.evidence.status["tests_passed"] = self.evidence.status.get("tests_passed", 0) + 1
                            
                    self.evidence.status["commands_run"].append(cmd_log)
                    self.evidence.write_status()
                    
                    logging.info(f"Exit Code: {exit_code}")
                    if result['stdout']: logging.info(f"Stdout:\n{result['stdout'][:500]}")
                    if result['stderr']: logging.info(f"Stderr:\n{result['stderr'][:500]}")
                    
                    if exit_code == 0:
                        if is_modifying:
                            gate_ok = self._post_patch_gate(task, cwd, iteration, is_retry, handle_failure)
                            if gate_ok:
                                is_retry = False
                                self.same_failure_count = 0
                                self.context.append({"role": "tool", "tool_call_id": tool_call.id, "name": "run_bash", "content": "Command succeeded.\n" + result["stdout"][-500:]})
                            else:
                                is_retry = True
                        else:
                            is_retry = False
                            self.same_failure_count = 0
                            self.context.append({"role": "tool", "tool_call_id": tool_call.id, "name": "run_bash", "content": "Command succeeded.\n" + result["stdout"][-500:]})
                    else:
                        is_retry = True
                        logging.info("Command failed! Pruning context and rolling back...")
                        if is_modifying:
                            self.rollback(cwd=cwd)
                        distilled_msg = self.distill_logs(exit_code, result["stdout"], result["stderr"])
                        failure_summary = f"Attempt failed when running `{cmd}`.\n{distilled_msg}"
                        handle_failure(failure_summary, f"Command `{cmd}` failed")

                elif name == "read_file" and args is not None:
                    tool_output = safe_read_file(
                        args.get("path") or args.get("filepath") or args.get("file"),
                        start_line=args.get("start_line", 1),
                        end_line=args.get("end_line"),
                        repo_root=self.repo_root,
                    )
                    logging.info(f"read_file -> {tool_output.splitlines()[0] if tool_output else 'empty'}")
                    self.context.append({"role": "tool", "tool_call_id": tool_call.id, "name": name, "content": tool_output})

                elif name == "apply_patch" and args is not None:
                    diff_text = args.get("diff") or args.get("patch") or ""
                    self.checkpoint(cwd=cwd)
                    patch_result = apply_patch_diff(diff_text, self.repo_root)
                    logging.info(f"apply_patch -> {patch_result.splitlines()[0] if patch_result else 'empty'}")
                    if patch_result.startswith("PATCH_ERROR"):
                        self.rollback(cwd=cwd)
                        is_retry = True
                        handle_failure(f"Patch application failed.\n{patch_result}", "apply_patch failed")
                    else:
                        gate_ok = self._post_patch_gate(task, cwd, iteration, is_retry, handle_failure)
                        if gate_ok:
                            is_retry = False
                            self.same_failure_count = 0
                            self.context.append({"role": "tool", "tool_call_id": tool_call.id, "name": name, "content": patch_result})
                        else:
                            is_retry = True

                elif args is None:
                    pass  # invalid-JSON TOOL_ERROR was already answered above

                else:
                    self.context.append({
                        "role": "tool", "tool_call_id": tool_call.id, "name": name,
                        "content": f"TOOL_ERROR: unknown tool '{name}'. Available tools: run_bash, read_file, apply_patch.",
                    })
                        
            except HarnessError as e:
                logging.error(f"Harness error encountered: {e}")
                is_retry = True
                try:
                    self.rollback(cwd=cwd)
                except Exception as rb_e:
                    logging.error(f"Fatal rollback error after harness error: {rb_e}")
                failure_summary = f"Internal system error occurred:\n{e}"
                handle_failure(failure_summary, f"HarnessError: {e}")
            except Exception as e:
                # An autonomous loop must degrade gracefully: log, roll
                # back, and let the model retry within the iteration cap.
                logging.exception(f"Unexpected error in agent loop: {e}")
                is_retry = True
                try:
                    self.rollback(cwd=cwd)
                except Exception as rb_e:
                    logging.error(f"Fatal rollback error after unexpected error: {rb_e}")
                handle_failure(f"Internal system error occurred:\n{e}", f"Unexpected error: {e}")
                
        return outcome, iteration

    def run_task(self, task: str):
        self.audit.log_event("task_started", {"task": task})
        logging.info(f"Starting task: {task}")
        task = self.enrich_task_with_github(task)
        
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
        task = self.enrich_task_with_github(task)
        
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
