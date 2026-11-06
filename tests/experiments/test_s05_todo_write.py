"""验证任务清单、状态更新和按轮次触发的提醒，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s05_todo_write as s05


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


def test_todo_update_renders_progress_and_replaces_list():
    todo = s05.TodoManager()
    items = [
        {"content": "Read", "status": "completed"},
        {"content": "Edit", "status": "in_progress"},
        {"content": "Test", "status": "pending"},
    ]
    assert todo.update(items) == "[x] Read\n[>] Edit\n[ ] Test\n\n(1/3 completed)"
    assert todo.update(json.dumps(items)) == todo.render()
    assert todo.update(str(items)) == todo.render()
    assert todo.update([]) == "No todos."


@pytest.mark.parametrize("items", [
    [{"content": "First", "status": "in_progress"}, {"content": "Second", "status": "in_progress"}],
    [{"content": "First", "status": "completed"}, {"content": "", "status": "pending"}],
    [{"content": "First", "status": "invalid"}],
    [{"content": "Too many", "status": "pending"}] * 21,
])
def test_invalid_update_preserves_previous_plan(items):
    todo = s05.TodoManager()
    original = [{"content": "Keep this plan", "status": "pending"}]
    todo.update(original)
    with pytest.raises(ValueError):
        todo.update(items)
    assert todo.items == original


def test_plan_and_file_tools_share_dispatch_and_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(s05, "WORKDIR", tmp_path)
    monkeypatch.setattr(s05, "TODO", s05.TodoManager())
    client = Mock()
    responses = iter([
        completion([tool("plan", "todo_write", todos=[{"content": "Write a.txt", "status": "in_progress"}])]),
        completion([tool("write", "write_file", path="a.txt", content="hello")]),
        completion([tool("finish", "todo_write", todos=[{"content": "Write a.txt", "status": "completed"}])]),
        completion(),
    ])
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    client.chat.completions.create.side_effect = create
    s05.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert requests[1][-1] == {
        "role": "tool", "tool_call_id": "plan", "content": "[>] Write a.txt\n\n(0/1 completed)",
    }
    assert requests[3][-1]["content"] == "[x] Write a.txt\n\n(1/1 completed)"
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert s05.TODO.items == [{"content": "Write a.txt", "status": "completed"}]


@pytest.mark.parametrize("rounds,reminder_requests", [
    (["glob"] * 3, [3]),
    (["glob", "glob", "todo_write", "glob", "glob", "glob"], [6]),
    (["glob"] * 6, [3, 6]),
])
def test_reminder_counts_rounds_and_resets_on_todo(tmp_path, monkeypatch, rounds, reminder_requests):
    monkeypatch.setattr(s05, "WORKDIR", tmp_path)
    monkeypatch.setattr(s05, "TODO", s05.TodoManager())
    responses = []
    for index, name in enumerate(rounds):
        if name == "todo_write":
            calls = [tool(str(index), name, todos=[{"content": "Test", "status": "pending"}])]
        else:
            calls = [tool(f"{index}-a", name, pattern="*.txt"),
                     tool(f"{index}-b", name, pattern="*.py")]
        responses.append(completion(calls))
    responses.append(completion())
    responses = iter(responses)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    s05.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert [index for index, messages in enumerate(requests)
            if messages[-1].get("content") == "<reminder>Update your todos.</reminder>"] == reminder_requests
    for index in reminder_requests:
        messages = requests[index]
        assert messages[-1]["role"] == "user"
        assert [message["tool_call_id"] for message in messages[-3:-1]] == [
            f"{index - 1}-a", f"{index - 1}-b",
        ]
