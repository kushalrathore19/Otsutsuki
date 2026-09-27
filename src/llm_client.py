import time
import json
import random
import re
from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError, APIError, AuthenticationError
import logging
from config import Config
from exceptions import LLMClientError

class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=0
        )
        self.usage_tokens = 0
        
    def _retry_call(self, func, *args, **kwargs):
        retries = 0
        max_retries = 20  # Increased heavily to survive shared org-level contention
        base_delay = 2.0
        while True:
            try:
                return func(*args, **kwargs)
            except AuthenticationError as e:
                logging.error(f"Authentication error: {e}")
                raise LLMClientError(f"Authentication failed: {e}") from e
            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                if retries >= max_retries:
                    logging.error(f"Max retries reached for LLM call: {e}")
                    raise LLMClientError(f"Max retries reached: {e}") from e
                
                delay = base_delay * (2 ** retries) + random.uniform(0, 1)
                if isinstance(e, RateLimitError):
                    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
                    if match:
                        minutes = int(match.group(1)) if match.group(1) else 0
                        seconds = float(match.group(2))
                        delay = (minutes * 60) + seconds + random.uniform(1.0, 10.0)
                
                logging.warning(f"Transient LLM error ({e}), retrying in {delay:.2f}s...")
                time.sleep(delay)
                retries += 1
            except APIError as e:
                logging.error(f"OpenAI API error: {e}")
                raise LLMClientError(f"OpenAI API error: {e}") from e
            except Exception as e:
                logging.error(f"Unexpected LLM error: {e}")
                raise LLMClientError(f"Unexpected LLM error: {e}") from e

    def critique(self, task: str) -> dict | None:
        messages = [
            {"role": "system", "content": "You are a senior software architect reviewing a coding task. Evaluate the task for viability, complexity, and better alternatives (e.g. using a standard library instead of custom logic). If you find a significantly better or cheaper approach, output a JSON object exactly like this: {'Original_Est_Tokens': 100, 'New_Est_Tokens': 50, 'Complexity': 'Low', 'Recommendation_Reason': 'reason', 'Recommended_Task': 'new task'}. If the original task is perfectly fine and requires no changes, output exactly the string 'PASS'."},
            {"role": "user", "content": f"Task: {task}"}
        ]
        try:
            response = self._retry_call(
                self.client.chat.completions.create,
                model=self.config.model,
                messages=messages,
                max_tokens=256
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
        except LLMClientError:
            return None
        except Exception as e:
            logging.error(f"Critique failed unexpectedly: {e}")
            return None
            
    def post_patch_critique(self, task: str, diff: str) -> dict:
        messages = [
            {"role": "system", "content": "You are a code reviewer. Review the provided diff against the original task description. Return a JSON object with 'decision' (either 'pass' or 'fail') and 'reason' (a short explanation). Fail if the code introduces obvious bugs, incomplete logic, or violates the task requirements."},
            {"role": "user", "content": f"Task:\n{task}\n\nDiff:\n{diff}"}
        ]
        try:
            response = self._retry_call(
                self.client.chat.completions.create,
                model=self.config.model,
                messages=messages,
                temperature=0.1,
                max_tokens=256
            )
            if response.usage:
                self.usage_tokens += response.usage.total_tokens
                
            content = response.choices[0].message.content or ""
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return {"decision": "pass", "reason": "Could not parse JSON, defaulting to pass"}
        except Exception as e:
            logging.error(f"Post-patch critique failed: {e}")
            return {"decision": "pass", "reason": f"Critique API error: {e}"}

    def step(self, messages, tools):
        response = self._retry_call(
            self.client.chat.completions.create,
            model=self.config.model,
            messages=messages,
            tools=tools,
            max_tokens=1024
        )
        if response.usage:
            self.usage_tokens += response.usage.total_tokens
        return response.choices[0].message
