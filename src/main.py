import os
import sys
import signal
import atexit
from config import Config
from orchestrator import Orchestrator
def main():
    try:
        config = Config.load()
        orchestrator = Orchestrator(config)
        def on_exit():
            if getattr(orchestrator, 'evidence', None) and orchestrator.evidence.status.get("state") == "running":
                orchestrator.evidence.status["state"] = "interrupted"
                orchestrator.evidence.write_outcome(config.iteration_cap)
        atexit.register(on_exit)
        def handle_signal(sig, frame):
            sys.exit(1)
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
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
            task = ""
        from tui import MissionControl
        app = MissionControl(orchestrator, task=task)
        app.run()
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
if __name__ == "__main__":
    main()
