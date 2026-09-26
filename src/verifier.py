import json
import logging
from llm_client import LLMClient

class Verifier:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        
    def verify(self, task: str, diff: str) -> dict:
        prompt = """You are a Verifier. Review the provided git diff against the ENTIRE original task.
Return ONLY a JSON object with this exact format:
{"decision": "pass", "reason": "...", "failed_subtask_id": null}
If the diff does not fully satisfy the task, return "decision": "fail", provide the reason, and optionally provide the "failed_subtask_id" of the subtask that needs to be re-run (integer or null).
Do not wrap in markdown blocks, output raw JSON only."""

        user_content = f"Original Task:\n{task}\n\nFull Diff against pre-run state:\n{diff}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content}
        ]
        
        logging.info("Verifier is reviewing the full diff...")
        response = self.llm.step(messages, [])
        content = response.content or ""
        
        try:
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                return json.loads(content[start:end+1])
        except Exception as e:
            logging.error(f"Verifier parsing failed: {e}")
            
        return {"decision": "fail", "reason": "Failed to parse verifier response", "failed_subtask_id": None}
