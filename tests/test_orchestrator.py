import sys
import os
import subprocess
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from orchestrator import Orchestrator
from config import Config

def test_critique_triggers():
    cfg = Config(api_key="test", model="test", base_url="test", iteration_cap=1, timeout_seconds=10, mem_limit_mb=1024, auto_approve=False, multi_agent=False, token_budget=50000, surgical_fraction=0.7, finalize_fraction=0.9)
    
    with patch("orchestrator.LLMClient") as mock_llm_cls, \
         patch("orchestrator.run_sandboxed") as mock_run_sandboxed, \
         patch("subprocess.run") as mock_subprocess_run:
         
        mock_llm = mock_llm_cls.return_value
        mock_llm.usage_tokens = 0
        mock_llm.critique.return_value = None
        
        mock_step_msg = MagicMock()
        mock_step_msg.content = "modifying"
        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_1"
        mock_tool_call.function.name = "run_bash"
        mock_tool_call.function.arguments = '{"command": "patch -p0 < /tmp/patch.diff"}'
        mock_step_msg.tool_calls = [mock_tool_call]
        mock_llm.step.return_value = mock_step_msg
        
        mock_run_sandboxed.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        
        multi_file_diff = "diff --git a/file1 b/file1\n+line1\ndiff --git a/file2 b/file2\n+line2"
        def subprocess_side_effect(*args, **kwargs):
            mock_res = MagicMock()
            if "git diff" in args[0]:
                mock_res.stdout = multi_file_diff
            else:
                mock_res.stdout = ""
            mock_res.returncode = 0
            return mock_res
            
        mock_subprocess_run.side_effect = subprocess_side_effect
        mock_llm.post_patch_critique.return_value = {"decision": "pass", "reason": "ok"}
        
        orch = Orchestrator(cfg)
        orch.run_task("do it")
        
        mock_llm.post_patch_critique.assert_called_once()
        
def test_critique_skips_trivial_patch():
    cfg = Config(api_key="test", model="test", base_url="test", iteration_cap=1, timeout_seconds=10, mem_limit_mb=1024, auto_approve=False, multi_agent=False, token_budget=50000, surgical_fraction=0.7, finalize_fraction=0.9)
    
    with patch("orchestrator.LLMClient") as mock_llm_cls, \
         patch("orchestrator.run_sandboxed") as mock_run_sandboxed, \
         patch("subprocess.run") as mock_subprocess_run:
         
        mock_llm = mock_llm_cls.return_value
        mock_llm.usage_tokens = 0
        mock_llm.critique.return_value = None
        
        mock_step_msg = MagicMock()
        mock_step_msg.content = "modifying"
        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_1"
        mock_tool_call.function.name = "run_bash"
        mock_tool_call.function.arguments = '{"command": "patch -p0 < /tmp/patch.diff"}'
        mock_step_msg.tool_calls = [mock_tool_call]
        mock_llm.step.return_value = mock_step_msg
        
        mock_run_sandboxed.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        
        single_file_diff = "diff --git a/file1 b/file1\n+line1\n-line2"
        def subprocess_side_effect(*args, **kwargs):
            mock_res = MagicMock()
            if "git diff" in args[0]:
                mock_res.stdout = single_file_diff
            else:
                mock_res.stdout = ""
            mock_res.returncode = 0
            return mock_res
            
        mock_subprocess_run.side_effect = subprocess_side_effect
        mock_llm.post_patch_critique.return_value = {"decision": "pass", "reason": "ok"}
        
        orch = Orchestrator(cfg)
        orch.run_task("do it")
        
        mock_llm.post_patch_critique.assert_not_called()
