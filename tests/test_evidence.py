import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from evidence import Evidence

def test_evidence_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        ev = Evidence(tmpdir)
        ev.status["iteration"] = 5
        ev.write_status()
        
        with open(os.path.join(tmpdir, "status.json"), "r") as f:
            data = json.load(f)
            
        assert data["iteration"] == 5
        assert data["state"] == "running"
