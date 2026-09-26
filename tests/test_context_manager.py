import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from context_manager import ContextManager

def test_prune_and_note():
    ctx = ContextManager([
        {"role": "user", "content": "do a task"},
        {"role": "assistant", "content": "I will do it", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "run_bash"}}]}
    ])
    ctx.prune_and_note("It failed")
    
    msgs = ctx.get_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "It failed"
    
def test_prune_and_note_with_tool_response():
    ctx = ContextManager([
        {"role": "user", "content": "do a task"},
        {"role": "assistant", "content": "I will do it", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "run_bash"}}]},
        {"role": "tool", "content": "Command succeeded"}
    ])
    ctx.prune_and_note("It failed")
    
    msgs = ctx.get_messages()
    assert len(msgs) == 2
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "It failed"
