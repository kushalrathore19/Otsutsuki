import os
import sys
from orchestrator import Orchestrator

def main():
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        print("Error: AI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    
    print("Harness Initialized")
    
    try:
        orchestrator = Orchestrator()
        task = "Create a file named hello.txt containing the word success"
        orchestrator.run_task(task)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
