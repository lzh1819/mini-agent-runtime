"""验证 S02 工具分发、顺序和文件边界，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s02_tool_use as s02


def completion(reason, calls=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason, "message": {
            "role": "assistant", "content": "done" if calls is None else None,
            "tool_calls": calls,
        }}],
    })


def tool(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def test_five_tools_execute_in_order_and_feed_results_back(tmp_path, monkeypatch):
    monkeypatch.setattr(s02, "WORKDIR", tmp_path)
    content = "你好\n你好\n"
    calls = [
        tool("write", "write_file", path="nested/a.txt", content=content),
        tool("read", "read_file", path="nested/a.txt", limit=1),
        tool("edit", "edit_file", path="nested/a.txt", old_text="你好", new_text="hello"),
        tool("reread", "read_file", path="nested/a.txt"),
        tool("glob", "glob", pattern="**/*.txt"),
        tool("bash", "bash", command="printf shell"),
    ]
    responses = iter([completion("tool_calls", calls), completion("stop")])
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    history = [{"role": "user", "content": "test"}]
    s02.agent_loop(history, client, "test")
    results = requests[1][2:]
    assert [result["tool_call_id"] for result in results] == [call["id"] for call in calls]
    assert [result["content"] for result in results] == [
        f"Wrote {len(content.encode('utf-8'))} bytes to nested/a.txt",
        "你好\n... (1 more lines)", "Edited nested/a.txt", "hello\n你好",
        "nested/a.txt", "shell",
    ]
    assert (tmp_path / "nested/a.txt").read_text() == "hello\n你好\n"
    assert requests[1][1]["tool_calls"] == calls
    assert history[-1] == {"role": "assistant", "content": "done"}


@pytest.mark.parametrize("via_symlink", [False, True])
def test_file_tools_reject_paths_outside_workspace(tmp_path, monkeypatch, via_symlink):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    monkeypatch.setattr(s02, "WORKDIR", workspace)
    path = "../outside.txt"
    if via_symlink:
        (workspace / "link").symlink_to(outside)
        path = "link"
    client = Mock()
    client.chat.completions.create.side_effect = [completion("tool_calls", [
        tool("read", "read_file", path=path),
        tool("write", "write_file", path=path, content="changed"),
        tool("edit", "edit_file", path=path, old_text="original", new_text="changed"),
        tool("glob", "glob", pattern=path),
        tool("absolute", "read_file", path=str(outside)),
    ]), completion("stop")]
    history = [{"role": "user", "content": "test"}]
    s02.agent_loop(history, client, "test")
    assert all(result["content"].startswith("Error: Path escapes workspace:")
               for result in history[2:-1])
    assert outside.read_text() == "original"


def test_failed_tools_return_errors_and_allow_next_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(s02, "WORKDIR", tmp_path)
    (tmp_path / "a.txt").write_text("original")
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion("tool_calls", [
            tool("unknown", "unknown"),
            tool("missing", "read_file", path="missing.txt"),
            tool("edit", "edit_file", path="a.txt", old_text="missing", new_text="changed"),
        ]), completion("stop"),
    ]
    history = [{"role": "user", "content": "test"}]
    s02.agent_loop(history, client, "test")
    assert all(result["content"].startswith("Error:") for result in history[2:-1])
    assert [result["tool_call_id"] for result in history[2:-1]] == ["unknown", "missing", "edit"]
    assert history[-1] == {"role": "assistant", "content": "done"}
    assert (tmp_path / "a.txt").read_text() == "original"


def test_truncated_response_does_not_execute_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(s02, "WORKDIR", tmp_path)
    client = Mock()
    client.chat.completions.create.return_value = completion("length", [
        tool("write", "write_file", path="a.txt", content="incomplete"),
    ])
    with pytest.raises(RuntimeError, match="length"):
        s02.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert not (tmp_path / "a.txt").exists()
