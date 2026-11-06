"""验证 S03 的直接放行、硬拒绝和用户审批，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s03_permission as s03


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


def test_workspace_tools_run_without_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(s03, "WORKDIR", tmp_path)
    approve = Mock(side_effect=AssertionError("Workspace operations should not prompt"))
    monkeypatch.setattr("builtins.input", approve)
    client = Mock()
    client.chat.completions.create.side_effect = [completion([
        tool("write", "write_file", path="a.txt", content="hello"),
        tool("read", "read_file", path="a.txt"),
        tool("glob", "glob", pattern="*.txt"),
        tool("bash", "bash", command="printf shell"),
    ]), completion()]
    history = [{"role": "user", "content": "test"}]
    s03.agent_loop(history, client, "test")
    assert [result["content"] for result in history[2:-1]] == [
        "Wrote 5 bytes to a.txt", "hello", "a.txt", "shell",
    ]
    approve.assert_not_called()


def test_hard_deny_never_prompts_or_executes(monkeypatch):
    approve = Mock(side_effect=AssertionError("Hard deny must not ask"))
    execute = Mock(side_effect=AssertionError("Hard deny must not execute"))
    monkeypatch.setattr("builtins.input", approve)
    monkeypatch.setitem(s03.TOOL_HANDLERS, "bash", execute)
    responses = iter([completion([tool("blocked", "bash", command="sudo echo test")]),
                      completion()])
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    s03.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert requests[1][-1] == {
        "role": "tool", "tool_call_id": "blocked", "content": "Permission denied.",
    }
    approve.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("answer", ["y", "yes", "n", ""])
@pytest.mark.parametrize("via_symlink", [False, True])
def test_outside_file_access_requires_approval(tmp_path, monkeypatch, answer, via_symlink):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    monkeypatch.setattr(s03, "WORKDIR", workspace)
    path = "../outside.txt"
    if via_symlink:
        (workspace / "link").symlink_to(outside)
        path = "link"
    approve = Mock(return_value=answer)
    monkeypatch.setattr("builtins.input", approve)
    client = Mock()
    client.chat.completions.create.side_effect = [completion([
        tool("write", "write_file", path=path, content="hello"),
        tool("edit", "edit_file", path=path, old_text="hello", new_text="changed"),
        tool("read", "read_file", path=path),
    ]), completion()]
    history = [{"role": "user", "content": "test"}]
    s03.agent_loop(history, client, "test")
    assert approve.call_count == 3
    assert [result["tool_call_id"] for result in history[2:-1]] == ["write", "edit", "read"]
    if answer in ("y", "yes"):
        assert outside.read_text() == "changed"
        assert history[-2]["content"] == "changed"
    else:
        assert outside.read_text() == "original"
        assert all(result["content"] == "Permission denied." for result in history[2:-1])


@pytest.mark.parametrize("command,needs_approval", [
    ("rm a.txt", True), ("DEL a.txt", True), ("echo ok && del a.txt", True),
    ("model", False), ("delimiter", False), ("echo del a.txt", False),
])
def test_destructive_command_rule(command, needs_approval):
    assert bool(s03.check_rules("bash", {"command": command})) is needs_approval
