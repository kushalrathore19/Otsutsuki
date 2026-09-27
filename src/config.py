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
    multi_agent: bool
    token_budget: int
    surgical_fraction: float
    finalize_fraction: float
    target_repo: str

    @classmethod
    def load(cls) -> "Config":
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env_path = os.path.join(repo_root, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        os.environ[key.strip()] = val.strip().strip("'").strip('"')
                        
        harness_yaml_path = os.path.join(repo_root, "harness.yaml")
        if os.path.exists(harness_yaml_path):
            import re
            def interpolate(val):
                if isinstance(val, str):
                    def replace_var(match):
                        var_name = match.group(1)
                        if var_name not in os.environ:
                            print(f"Error: Required environment variable '{var_name}' is missing.")
                            sys.exit(1)
                        return os.environ[var_name]
                    return re.sub(r'\$\{([^}]+)\}', replace_var, val)
                return val

            try:
                import yaml
                with open(harness_yaml_path, "r") as f:
                    data = yaml.safe_load(f) or {}
                if "model" in data:
                    os.environ["AI_MODEL"] = interpolate(str(data["model"]))
                if "base_url" in data:
                    os.environ["AI_BASE_URL"] = interpolate(str(data["base_url"]))
            except ImportError:
                with open(harness_yaml_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("model:"):
                            val = line.split(":", 1)[1].strip().strip("\"'")
                            os.environ["AI_MODEL"] = interpolate(val)
                        elif line.startswith("base_url:"):
                            val = line.split(":", 1)[1].strip().strip("\"'")
                            os.environ["AI_BASE_URL"] = interpolate(val)

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
            auto_approve=os.environ.get("AUTO_APPROVE", "0") == "1",
            multi_agent=os.environ.get("MULTI_AGENT", "0") == "1",
            token_budget=int(os.environ.get("TOKEN_BUDGET", 50000)),
            surgical_fraction=float(os.environ.get("SURGICAL_FRACTION", 0.7)),
            finalize_fraction=float(os.environ.get("FINALIZE_FRACTION", 0.9)),
            target_repo=os.environ.get("TARGET_REPO", repo_root)
        )
