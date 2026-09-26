import os
import sys
from dataclasses import dataclass

@dataclass
class Config:
    api_key: str
    model: str
    base_url: str
    iteration_cap: int
    timeout_seconds: int
    mem_limit_mb: int
    auto_approve: bool

    @classmethod
    def load(cls) -> "Config":
        api_key = os.environ.get("AI_API_KEY")
        if not api_key:
            print("Error: AI_API_KEY environment variable is missing.")
            sys.exit(1)
            
        return cls(
            api_key=api_key,
            model=os.environ.get("AI_MODEL", "openai/gpt-oss-20b"),
            base_url=os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1"),
            iteration_cap=int(os.environ.get("ITERATION_CAP", 8)),
            timeout_seconds=int(os.environ.get("TIMEOUT_SECONDS", 30)),
            mem_limit_mb=int(os.environ.get("MEM_LIMIT_MB", 1024)),
            auto_approve=os.environ.get("AUTO_APPROVE", "0") == "1"
        )
