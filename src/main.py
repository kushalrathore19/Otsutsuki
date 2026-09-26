import os
import sys
from orchestrator import Orchestrator

def main():
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        print("Error: AI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    
    try:
        orchestrator = Orchestrator()
        
        task = None
        if len(sys.argv) > 1:
            task = sys.argv[1]
        elif os.environ.get("TASK"):
            task = os.environ.get("TASK")
        else:
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            task_file = os.path.join(repo_root, "TASK_INPUT.md")
            if os.path.exists(task_file):
                with open(task_file, "r") as f:
                    task = f.read().strip()
                    
        if not task:
            print("Error: No task provided. Please provide a task via CLI argument, TASK environment variable, or a TASK_INPUT.md file in the repository root.", file=sys.stderr)
            sys.exit(1)
            
        # Boot directly into Textual TUI
        from tui import MissionControl
        app = MissionControl(orchestrator, task=task)
        app.run()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
