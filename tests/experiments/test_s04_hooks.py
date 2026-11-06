"""验证 Hook 的执行顺序、权限拦截和继续运行，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s04_hooks as s04


def completion(calls=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else "done"}}],
    })


def tool(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def test_hooks_run_in_order_around_real_tool_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(s04, "WORKDIR", tmp_path)
    monkeypatch.setattr(s04, "HOOKS", {event: [] for event in s04.HOOKS})
    events = []
    s04.register_hook("UserPromptSubmit", lambda query: events.append(("input", query)))
    s04.register_hook("PreToolUse", s04.permission_hook)
    s04.register_hook("PreToolUse", lambda name, args: events.append(("before", name)))
    s04.register_hook("PostToolUse", lambda name, args, output: events.append(("after", name, output)))
    s04.register_hook("Stop", lambda messages: events.append(("stop", messages[-1]["content"])))
    client = Mock()
    client.chat.completions.create.side_effect = [completion([
        tool("write", "write_file", path="a.txt", content="hello"),
        tool("read", "read_file", path="a.txt"),
    ]), completion()]
    s04.trigger_hooks("UserPromptSubmit", "test")
    history = [{"role": "user", "content": "test"}]
    s04.agent_loop(history, client, "test")
    assert events == [
        ("input", "test"), ("before", "write_file"),
        ("after", "write_file", "Wrote 5 bytes to a.txt"),
        ("before", "read_file"), ("after", "read_file", "hello"), ("stop", "done"),
    ]
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert [result["tool_call_id"] for result in history[2:-1]] == ["write", "read"]


def test_permission_hook_blocks_later_hooks_and_execution(monkeypatch):
    later = Mock(side_effect=AssertionError("Later pre-hook must not run"))
    after = Mock(side_effect=AssertionError("Post-hook must not run for blocked tools"))
    execute = Mock(side_effect=AssertionError("Blocked tool must not execute"))
    monkeypatch.setattr(s04, "HOOKS", {
        "UserPromptSubmit": [], "PreToolUse": [s04.permission_hook, later],
        "PostToolUse": [after], "Stop": [],
    })
    monkeypatch.setitem(s04.TOOL_HANDLERS, "bash", execute)
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion([tool("blocked", "bash", command="sudo echo test")]), completion(),
    ]
    history = [{"role": "user", "content": "test"}]
    s04.agent_loop(history, client, "test")
    assert history[2] == {
        "role": "tool", "tool_call_id": "blocked", "content": "Permission denied.",
    }
    later.assert_not_called()
    after.assert_not_called()
    execute.assert_not_called()


def test_stop_hook_injects_message_and_continues(monkeypatch):
    stop = Mock(side_effect=["Check the result once more.", None])
    monkeypatch.setattr(s04, "HOOKS", {
        "UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": [stop],
    })
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return completion()

    client = Mock()
    client.chat.completions.create.side_effect = create
    history = [{"role": "user", "content": "test"}]
    s04.agent_loop(history, client, "test")
    assert len(requests) == 2
    assert requests[1][-1] == {"role": "user", "content": "Check the result once more."}
    assert stop.call_count == 2
    assert history[-1] == {"role": "assistant", "content": "done"}


def test_hook_errors_surface_before_tool_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(s04, "WORKDIR", tmp_path)
    broken = Mock(side_effect=ValueError("hook failed"))
    monkeypatch.setattr(s04, "HOOKS", {
        "UserPromptSubmit": [], "PreToolUse": [broken], "PostToolUse": [], "Stop": [],
    })
    client = Mock()
    client.chat.completions.create.return_value = completion([
        tool("write", "write_file", path="a.txt", content="hello"),
    ])
    with pytest.raises(ValueError, match="hook failed"):
        s04.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert not (tmp_path / "a.txt").exists()
