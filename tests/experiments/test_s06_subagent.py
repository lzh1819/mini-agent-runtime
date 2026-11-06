"""验证子任务的上下文边界、共享工具和生命周期，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s06_subagent as s06


def completion(calls=None, content="done", finish_reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0,
                     "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else content}}],
    })


def tool(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def fake_client(responses):
    responses = iter(responses)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    return client, requests


def test_task_isolates_messages_but_shares_files(tmp_path, monkeypatch):
    monkeypatch.setattr(s06, "WORKDIR", tmp_path)
    client, requests = fake_client([
        completion([tool("delegate", "task", prompt="Write a.txt with hello, then read it.")]),
        completion([tool("child-write", "write_file", path="a.txt", content="hello")]),
        completion([tool("child-read", "read_file", path="a.txt")]),
        completion(content="Created a.txt and checked its contents."),
        completion([tool("parent-read", "read_file", path="a.txt")]),
        completion(content="Checked the result."),
    ])
    messages = [
        {"role": "system", "content": s06.SYSTEM},
        {"role": "user", "content": "Parent-only context: finish the larger task."},
    ]

    s06.agent_loop(messages, client, "test-model")

    assert requests[1]["messages"] == [
        {"role": "system", "content": s06.SUB_SYSTEM},
        {"role": "user", "content": "Write a.txt with hello, then read it."},
    ]
    assert requests[3]["messages"][-1] == {
        "role": "tool", "tool_call_id": "child-read", "content": "hello",
    }
    assert requests[4]["messages"][-1] == {
        "role": "tool", "tool_call_id": "delegate",
        "content": "Created a.txt and checked its contents.",
    }
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "delegate", "parent-read",
    ]
    assert messages[-2]["content"] == "hello"
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert all(request["model"] == "test-model" for request in requests)


def test_child_tools_cannot_bypass_permission_hooks(monkeypatch):
    bash = Mock()
    post = Mock()
    monkeypatch.setitem(s06.SUB_HANDLERS, "bash", bash)
    monkeypatch.setitem(s06.HOOKS, "PostToolUse", [post])
    client, requests = fake_client([
        completion([tool("denied", "bash", command="sudo echo test")]),
        completion(content="Permission was denied."),
    ])

    assert s06.run_subagent("Run a command.", client, "test") == "Permission was denied."
    assert requests[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "denied", "content": "Permission denied.",
    }
    bash.assert_not_called()
    post.assert_not_called()


def test_child_has_no_task_tool_or_recursive_dispatch():
    client, requests = fake_client([
        completion([tool("recursive", "task", prompt="Delegate again.")]),
        completion(content="Finished without delegating."),
    ])

    s06.run_subagent("Inspect a file.", client, "test")

    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {
        "bash", "read_file", "write_file", "edit_file", "glob",
    }
    assert requests[1]["messages"][-1]["content"] == "Error: Unknown tool: task"
    assert client.chat.completions.create.call_count == 2


def test_child_stop_hook_can_request_more_work(monkeypatch):
    stop = Mock(side_effect=["Check the conclusion once more.", None])
    monkeypatch.setitem(s06.HOOKS, "Stop", [stop])
    client, requests = fake_client([
        completion(content="First conclusion."),
        completion(content="Verified conclusion."),
    ])

    assert s06.run_subagent("Inspect the project.", client, "test") == "Verified conclusion."
    assert requests[1]["messages"][-1] == {
        "role": "user", "content": "Check the conclusion once more.",
    }
    assert stop.call_count == 2


def test_child_call_limit_surfaces_as_error(monkeypatch):
    monkeypatch.setattr(s06, "SUB_MAX_TURNS", 1)
    client, _ = fake_client([completion([tool("read", "glob", pattern="*.txt")])])

    with pytest.raises(RuntimeError, match="Subagent model call limit reached"):
        s06.run_subagent("Keep exploring.", client, "test")
    assert client.chat.completions.create.call_count == 1


def test_truncated_child_call_does_not_execute(monkeypatch):
    write = Mock()
    monkeypatch.setitem(s06.SUB_HANDLERS, "write_file", write)
    client, _ = fake_client([
        completion([tool("truncated", "write_file", path="a.txt", content="hello")],
                   finish_reason="length"),
    ])

    with pytest.raises(RuntimeError, match="Model stopped unexpectedly: length"):
        s06.run_subagent("Write a file.", client, "test")
    write.assert_not_called()
