import os
import json
import threading
import time

class AuditLog:
    def __init__(self, repo_root: str):
        self.log_path = os.path.join(repo_root, "events.jsonl")
        self.lock = threading.Lock()
        
    def log_event(self, event_type: str, data: dict = None):
        if data is None:
            data = {}
        with self.lock:
            try:
                with open(self.log_path, "a") as f:
                    entry = {"timestamp": time.time(), "event": event_type, **data}
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass
