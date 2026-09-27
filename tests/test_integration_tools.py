"""End-to-end tests: a mocked LLM drives the real tool loop.

These exercise the full dispatch path in orchestrator._execute_subtask
(read_file / apply_patch / run_bash), the checkpoint/rollback machinery on
a real git repo, and the patch self-correction loop (PATCH_ERROR feedback
followed by a successful retry).
"""

import sys
import os
import json
import subprocess
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from orchestrator import Orchestrator
from config import Config


def _git_repo(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    subprocess.run("git init -q", shell=True, cwd=str(root), check=True)
    subprocess.run(
        "git -c user.email=t@t.io -c user.name=T commit --allow-empty -q -m init",
        shell=True, cwd=str(root), check=True,
    )
    (root / "app.py").write_text("def greet(name):\n    return 'hello ' + name\n")
    return root


def _config(root):
    return Config(
        api_key="test", model="test", base_url="test", iteration_cap=5,
        timeout_seconds=10, mem_limit_mb=1024, auto_approve=True,
        multi_agent=False, token_budget=50000, surgical_fraction=0.7,
        finalize_fraction=0.9, target_repo=str(root),
    )


def _msg(content=None, tool_name=None, arguments=None):
    m = MagicMock()
    m.content = content
    if tool_name is None:
        m.tool_calls = []
    else:
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = tool_name
        tc.function.arguments = json.dumps(arguments)
        m.tool_calls = [tc]
    return m


def _run_orchestrator(root, steps):
    with patch("orchestrator.LLMClient") as mock_llm_cls:
        llm = mock_llm_cls.return_value
        llm.usage_tokens = 0
        llm.critique.return_value = None
        llm.step.side_effect = steps
        orch = Orchestrator(_config(root))
        orch.run_task("create hello.txt containing success")
        return orch


def test_tool_loop_read_patch_verify(tmp_path):
    root = _git_repo(tmp_path)
    diff = "--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+success\n"

    orch = _run_orchestrator(root, [
        _msg(tool_name="read_file", arguments={"path": "app.py", "start_line": 1, "end_line": 10}),
        _msg(tool_name="apply_patch", arguments={"diff": diff}),
        _msg(tool_name="run_bash", arguments={"command": "cat hello.txt"}),
        _msg(content="verified <status>TASK_COMPLETE</status>"),
    ])

    assert orch.evidence.status["state"] == "complete"
    assert (root / "hello.txt").read_text() == "success\n"

    # The read_file tool response must carry line numbers, and apply_patch
    # must have reported success back into the conversation.
    tool_msgs = [m for m in orch.context.get_messages() if m.get("role") == "tool"]
    assert any("app.py" in m["content"] and "def greet" in m["content"] for m in tool_msgs)
    assert any(m["content"].startswith("OK") and "hello.txt created" in m["content"] for m in tool_msgs)
    assert any("success" in m["content"] for m in tool_msgs)


def test_patch_error_feedback_enables_self_correction(tmp_path):
    root = _git_repo(tmp_path)
    bad_diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -3,3 +3,3 @@\n"
        " def nonexistent_function():\n"
        "-    return 'hello ' + name\n"
        "+    return 'hi ' + name\n"
    )
    good_diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def greet(name):\n"
        "-    return 'hello ' + name\n"
        "+    return 'hi ' + name\n"
    )

    orch = _run_orchestrator(root, [
        _msg(tool_name="apply_patch", arguments={"diff": bad_diff}),
        _msg(tool_name="apply_patch", arguments={"diff": good_diff}),
        _msg(content="<status>TASK_COMPLETE</status>"),
    ])

    assert orch.evidence.status["state"] == "complete"
    assert "return 'hi ' + name" in (root / "app.py").read_text()

    all_msgs = orch.context.get_messages()
    # The failed attempt fed a parseable PATCH_ERROR back to the model
    # (via the pruned-failure note the orchestrator substitutes for the
    # dead exchange), and the repo was rolled back before the retry.
    assert any("PATCH_ERROR" in m["content"] and "app.py" in m["content"] for m in all_msgs)
    assert orch.evidence.status["rollback_count"] == 1


def test_blocked_command_reported_not_executed(tmp_path):
    root = _git_repo(tmp_path)

    orch = _run_orchestrator(root, [
        _msg(tool_name="run_bash", arguments={"command": "rm -rf /"}),
        _msg(content="<status>TASK_COMPLETE</status>"),
    ])

    all_msgs = orch.context.get_messages()
    assert any("Security violation" in m["content"] for m in all_msgs)
    assert orch.evidence.status["state"] == "complete"


def test_minified_file_refusal_reaches_model(tmp_path):
    root = _git_repo(tmp_path)
    (root / "bundle.js").write_text("var a=1;" + "".join(f"x{i}={i};" for i in range(5000)))

    orch = _run_orchestrator(root, [
        _msg(tool_name="read_file", arguments={"path": "bundle.js"}),
        _msg(content="<status>TASK_COMPLETE</status>"),
    ])

    tool_msgs = [m for m in orch.context.get_messages() if m.get("role") == "tool"]
    assert any(m["content"].startswith("READ_ERROR") and "minified" in m["content"] for m in tool_msgs)
