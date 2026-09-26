import os
import json
import threading

class Evidence:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.lock = threading.Lock()
        self.status = {
            "task": "",
            "iteration": 0,
            "commands_run": [],
            "rollback_count": 0,
            "state": "running",
            "tokens": 0
        }
        
    def write_task(self, task: str):
        with self.lock:
            self.status["task"] = task
        with open(os.path.join(self.repo_root, "TASK.md"), "w") as f:
            f.write(task)
        self.write_status()
            
    def write_status(self):
        import copy
        with self.lock:
            status_copy = copy.deepcopy(self.status)
        with open(os.path.join(self.repo_root, "status.json"), "w") as f:
            json.dump(status_copy, f, indent=2)
            
    def write_outcome(self, max_iterations: int):
        self.write_status()
        with open(os.path.join(self.repo_root, "OUTCOME.md"), "w") as f:
            f.write(f"# Run Outcome\n\n")
            f.write(f"**Status:** {self.status['state']}\n")
            f.write(f"**Iterations:** {self.status['iteration']} / {max_iterations}\n")
            f.write(f"**Rollbacks:** {self.status['rollback_count']}\n\n")
            f.write("## Commands Run\n")
            for c in self.status["commands_run"]:
                f.write(f"- Iteration {c['iteration']}: `{c['command']}` (Exit: {c.get('exit_code', 'N/A')})\n")
            if "reviewer_notes" in self.status:
                f.write(f"\n## Reviewer Notes (non-blocking)\n\n{self.status['reviewer_notes']}\n")

    def archive(self):
        import shutil
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        run_dir = os.path.join(self.repo_root, "runs", timestamp)
        os.makedirs(run_dir, exist_ok=True)
        for f in ["TASK.md", "status.json", "OUTCOME.md"]:
            src = os.path.join(self.repo_root, f)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(run_dir, f))
        return timestamp
