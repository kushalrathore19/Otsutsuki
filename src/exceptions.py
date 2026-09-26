class HarnessError(Exception):
    pass
class SandboxError(HarnessError):
    pass
class PatchApplyError(HarnessError):
    pass
class RollbackError(HarnessError):
    pass
class LLMClientError(HarnessError):
    pass
