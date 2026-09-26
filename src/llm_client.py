import json
import re
from openai import OpenAI
import logging
from config import Config

class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        )
        self.usage_tokens = 0
        
    def critique(self, task: str) -> dict | None:
        messages = [
            {"role": "system", "content": "You are a senior software architect reviewing a coding task. Evaluate the task for viability, complexity, and better alternatives (e.g. using a standard library instead of custom logic). If you find a significantly better or cheaper approach, output a JSON object exactly like this: {'Original_Est_Tokens': 100, 'New_Est_Tokens': 50, 'Complexity': 'Low', 'Recommendation_Reason': 'reason', 'Recommended_Task': 'new task'}. If the original task is perfectly fine and requires no changes, output exactly the string 'PASS'."},
            {"role": "user", "content": f"Task: {task}"}
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages
            )
            if response.usage:
                self.usage_tokens += response.usage.total_tokens
                
            content = response.choices[0].message.content or ""
            if "PASS" in content.upper() and "{" not in content:
                return None
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return None
        except Exception as e:
            logging.error(f"Critique failed: {e}")
            return None
            
    def step(self, messages, tools):
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools
        )
        if response.usage:
            self.usage_tokens += response.usage.total_tokens
        return response.choices[0].message
