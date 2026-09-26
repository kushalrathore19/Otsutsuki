import json
import logging
from llm_client import LLMClient

class Architect:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        
    def plan(self, task: str) -> list:
        prompt = """You are an Architect. Break the given task into 2-6 concrete, ordered subtasks.
Each must be specific enough to verify independently (e.g. "Add function X to file Y", "Write test for X").
Return ONLY a JSON list of objects, exactly like this:
[{"id": 1, "description": "...", "done": false, "depends_on": [], "expected_files": ["path/to/file"]}]
If the task is already atomic (cannot usefully be split), return a single-item list.
Subtasks with no shared expected_files and no dependency relation between them are parallelizable.
Do not wrap in markdown blocks, output raw JSON only."""
        
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task}
        ]
        
        logging.info("Architect is planning subtasks...")
        response = self.llm.step(messages, [])
        content = response.content or ""
        
        try:
            start = content.find("[")
            end = content.rfind("]")
            if start != -1 and end != -1:
                return json.loads(content[start:end+1])
        except Exception as e:
            logging.error(f"Architect parsing failed: {e}")
            
        return [{"id": 1, "description": task, "done": False, "depends_on": [], "expected_files": []}]
