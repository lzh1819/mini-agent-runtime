"""验证消息闭环，不请求外部 API。

运行：uv run python -m pytest tests/experiments/test_s01_agent_loop.py -q
"""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s01_agent_loop as s01


def completion(reason, calls=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason, "message": {
            "role": "assistant", "content": "done" if calls is None else None,
            "tool_calls": calls,
        }}],
    })


def tool(call_id, command):
    return {"id": call_id, "type": "function", "function": {
        "name": "bash", "arguments": s01.json.dumps({"command": command}),
    }}


def test_tool_results_reach_next_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses = iter([
        completion("tool_calls", [tool("a", "printf first"), tool("b", "printf second")]),
        completion("tool_calls", [tool("c", "printf third")]),
        completion("stop"),
    ])
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    history = [{"role": "system", "content": s01.SYSTEM},
               {"role": "user", "content": "test"}]
    s01.agent_loop(history, client, "test")
    assert len(requests) == 3
    assert requests[1][2]["tool_calls"][0]["id"] == "a"
    assert requests[1][3:] == [
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
    ]
    assert requests[2][-1] == {"role": "tool", "tool_call_id": "c", "content": "third"}
    assert history[-1] == {"role": "assistant", "content": "done"}


@pytest.mark.parametrize("case", ["length", "unknown", "arguments", "limit"])
def test_failures_do_not_look_like_success(case, monkeypatch):
    call = tool("a", "printf test")
    if case == "unknown":
        call["function"]["name"] = "unknown"
    if case == "arguments":
        call["function"]["arguments"] = '{"command": 123}'
    client = Mock()
    client.chat.completions.create.return_value = completion(
        "length" if case == "length" else "tool_calls", [call],
    )
    execute = Mock(return_value="test")
    monkeypatch.setattr(s01, "run_bash", execute)
    monkeypatch.setattr(s01, "MAX_TURNS", 1)
    with pytest.raises((RuntimeError, ValueError)):
        s01.agent_loop([{"role": "user", "content": "test"}], client, "test")
    assert execute.call_count == (1 if case == "limit" else 0)
    assert client.chat.completions.create.call_count == 1
