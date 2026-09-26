import os
import json

class Evidence:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.status = {
            "task": "",
            "iteration": 0,
            "commands_run": [],
            "rollback_count": 0,
            "state": "running",
            "tokens": 0
        }
        
    def write_task(self, task: str):
        self.status["task"] = task
        with open(os.path.join(self.repo_root, "TASK.md"), "w") as f:
            f.write(task)
        self.write_status()
            
    def write_status(self):
        with open(os.path.join(self.repo_root, "status.json"), "w") as f:
            json.dump(self.status, f, indent=2)
            
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
