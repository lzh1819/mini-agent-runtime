"""验证 Goal 的继续、停止和 OpenAI 消息边界，不请求真实模型。"""

import asyncio
import json
import shlex
import sys
from unittest.mock import Mock

import httpx2
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion

from experiments import s17_goal_loop as s17


def completion(text=None, calls=None, reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "content": text, "tool_calls": calls}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def tool(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def evaluation(ok=False, reason="Missing test output", impossible=False):
    return completion(json.dumps({"ok": ok, "reason": reason, "impossible": impossible}))


@pytest.fixture
def api():
    clients = []

    def create(responses):
        responses = iter(responses)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            response = next(responses)
            if isinstance(response, httpx2.Response):
                return response
            return httpx2.Response(200, json=response.model_dump(exclude_none=True))

        client = OpenAI(api_key="test-key", base_url="https://example.test/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
        clients.append(client)
        return client, requests

    yield create
    for client in clients:
        client.close()


def make_session(tmp_path, api, replies, *, block_cap=8, **kwargs):
    client, requests = api(replies)
    evaluator = s17.PromptGoalEvaluator(client, "evaluator-model")
    goal = s17.GoalController(evaluator, block_cap=block_cap)
    return s17.AgentSession(client, "worker-model", goal, tmp_path, **kwargs), requests


def test_unmet_goal_continues_and_real_tool_results_reach_evaluator(tmp_path, api):
    command = f"{shlex.quote(sys.executable)} check.py"
    session, requests = make_session(tmp_path, api, [
        completion("Implementation done"), evaluation(),
        completion(calls=[
            tool("write", "write_file", path="check.py", content="print('old')\n"),
            tool("edit", "edit_file", path="check.py", old_text="old", new_text="verified"),
            tool("read", "read_file", path="check.py"),
            tool("find", "glob", pattern="**/*.py"),
            tool("run", "bash", command=command),
        ]),
        completion("The command printed verified and exited with code 0"),
        evaluation(ok=True, reason="The command result has exit_code=0"),
    ])
    result = asyncio.run(session.submit("/goal check.py prints verified and exits with code 0"))
    assert result.status == "achieved"
    assert session.goal.active is None
    assert session.goal.last_status["met"] is True
    assert (tmp_path / "check.py").read_text() == "print('verified')\n"
    assert session.total_tokens == 45  # Worker usage; evaluator requests are separate.
    assert [request["model"] for request in requests] == [
        "worker-model", "evaluator-model", "worker-model", "worker-model", "evaluator-model",
    ]
    assert "[Goal still active]" in requests[2]["messages"][-1]["content"]
    assert "Missing test output" in requests[2]["messages"][-1]["content"]
    names = {item["function"]["name"] for item in requests[0]["tools"]}
    assert names == {"bash", "read_file", "write_file", "edit_file", "glob"}
    results = requests[3]["messages"][-5:]
    assert [item["tool_call_id"] for item in results] == ["write", "edit", "read", "find", "run"]
    assert all(item["role"] == "tool" for item in results)
    assert results[-1]["content"] == "exit_code=0\nverified"
    for request in (requests[1], requests[-1]):
        assert "tools" not in request
        assert request["max_completion_tokens"] == s17.DEFAULT_EVALUATOR_MAX_TOKENS
        assert "independent completion evaluator" in request["messages"][0]["content"]
    assert "exit_code=0" in requests[-1]["messages"][-1]["content"]


@pytest.mark.parametrize("status,reply,remains_active", [
    ("failed", evaluation(impossible=True, reason="Required service is unavailable"), False),
    ("error", completion("not JSON"), True),
    ("error", completion('{"ok":true,"reason":"conflict","impossible":true}'), True),
    ("error", httpx2.Response(503, json={"error": {"message": "unavailable"}}), True),
])
def test_impossible_or_invalid_evaluation_returns_control(tmp_path, api, status, reply, remains_active):
    session, requests = make_session(tmp_path, api, [completion("Stopped here"), reply])
    result = asyncio.run(session.submit("/goal service is ready"))
    assert result.status == status
    assert bool(session.goal.active) is remains_active
    assert result.reason
    assert len(requests) == 2
    assert not session.goal.last_status["met"]
    if status == "failed":
        assert session.goal.last_status["failed"]


@pytest.mark.parametrize("global_limit", [False, True])
def test_limits_keep_goal_active_and_next_query_can_continue(tmp_path, api, global_limit):
    attempts = 1 if global_limit else 3
    replies = [reply for _ in range(attempts) for reply in (completion("Not finished"), evaluation())]
    replies.extend([completion("Now finished"), evaluation(ok=True, reason="Evidence supplied")])
    session, requests = make_session(tmp_path, api, replies, block_cap=2,
                                     max_turns=1 if global_limit else None)

    async def exercise():
        limited = await session.submit("/goal tests pass")
        assert limited.status == ("max_turns" if global_limit else "limit")
        assert session.goal.active is not None
        assert session.goal.active.iterations == attempts
        assert not session.goal.last_status["met"]
        return await session.submit("Here is the missing evidence; continue")

    assert asyncio.run(exercise()).status == "achieved"
    assert len(requests) == (attempts + 1) * 2


def test_background_result_resumes_deferred_goal(tmp_path, api):
    running = True
    session, requests = make_session(tmp_path, api, [
        completion("Waiting for the background result"),
        completion("The background result is ready"),
        evaluation(ok=True, reason="Background result reports exit_code=0"),
    ], background_running=lambda: running)

    async def exercise():
        nonlocal running
        deferred = await session.submit("/goal background check exits with code 0")
        assert deferred.status == "defer"
        assert session.goal.active is not None
        assert len(requests) == 1
        running = False
        return await session.submit_background_result("check passed; exit_code=0")

    assert asyncio.run(exercise()).status == "achieved"
    assert len(requests) == 3
    assert "[Background task completed]" in requests[1]["messages"][-1]["content"]
    assert "check passed; exit_code=0" in requests[-1]["messages"][-1]["content"]


def test_status_replacement_clear_and_restore_do_not_invent_work(tmp_path, api):
    session, requests = make_session(tmp_path, api, [
        completion("First attempt"), evaluation(), completion("Second attempt"), evaluation(),
    ], max_turns=1)

    async def exercise():
        assert (await session.submit("/goal first condition")).status == "max_turns"
        status = await session.submit("/goal")
        assert status.status == "status"
        assert "Goal active: first condition" in status.text
        assert "Tokens: 15" in status.text
        assert len(requests) == 2
        assert (await session.submit("/goal replacement condition")).status == "max_turns"
        assert session.goal.active.condition == "replacement condition"
        assert any(event["reason"] == "replaced by a new goal" for event in session.goal.events)
        restored = s17.GoalController.restore(session.goal.evaluator, session.goal.events)
        assert restored.active.condition == "replacement condition"
        assert restored.active.iterations == 0
        for alias in sorted(s17.CLEAR_ALIASES):
            session.goal.set_goal("clear me")
            assert (await session.submit(f"/goal {alias}")).status == "cleared"
            assert session.goal.active is None
        assert s17.GoalController.restore(session.goal.evaluator, session.goal.events).active is None
        notification = await session.submit_background_result("late result")
        assert notification.status == "background_result"

    asyncio.run(exercise())
    assert len(requests) == 4


def test_ordinary_turn_skips_evaluator_and_permissions_still_apply(tmp_path, monkeypatch, api):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private")
    (tmp_path / "link.txt").symlink_to(outside)
    monkeypatch.setattr("builtins.input", Mock(return_value="n"))
    session, requests = make_session(tmp_path, api, [
        completion(calls=[
            tool("outside", "write_file", path="../outside.txt", content="overwrite"),
            tool("link", "read_file", path="link.txt"),
            tool("deny", "bash", command="sudo echo forbidden"),
            tool("confirm", "bash", command="rm protected.txt"),
        ]),
        completion("Blocked operations"),
    ])
    result = asyncio.run(session.submit("Inspect permissions"))
    assert result.status == "allow"
    assert outside.read_text() == "private"
    assert len(requests) == 2
    assert all("Permission denied" in message["content"] for message in requests[-1]["messages"][-4:])


@pytest.mark.parametrize("role", ["worker", "evaluator"])
def test_truncated_responses_cannot_execute_calls_or_complete_goal(tmp_path, api, role):
    truncated = completion('{"ok":true,"reason":"unfinished"', calls=[{
        "id": "partial", "type": "function", "function": {
            "name": "write_file", "arguments": '{"path":"should-not-exist.txt",',
        },
    }], reason="length")
    replies = [truncated] if role == "worker" else [completion("Done"), truncated]
    session, requests = make_session(tmp_path, api, replies)
    if role == "worker":
        with pytest.raises(s17.GoalError, match="length"):
            asyncio.run(session.submit("/goal finish work"))
    else:
        result = asyncio.run(session.submit("/goal finish work"))
        assert result.status == "error"
        assert "length" in result.reason
    assert session.goal.active is not None
    assert not session.goal.last_status["met"]
    assert not (tmp_path / "should-not-exist.txt").exists()
    assert len(requests) == len(replies)


def test_factory_loads_project_env_and_cli_quits_without_model_calls(tmp_path, monkeypatch):
    load_env = Mock()
    client = Mock()
    monkeypatch.setattr(s17, "load_dotenv", load_env)
    monkeypatch.setattr(s17, "OpenAI", Mock(return_value=client))
    monkeypatch.setenv("MODEL_ID", "worker-model")
    monkeypatch.setenv("GOAL_EVALUATOR_MODEL_ID", "judge-model")
    monkeypatch.setenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "3")
    monkeypatch.setenv("MAX_TURNS", "5")
    session = s17.make_live_session(tmp_path)
    load_env.assert_called_once_with(s17.Path(s17.__file__).resolve().parents[1] / ".env")
    s17.OpenAI.assert_called_once_with()
    assert session.model == "worker-model"
    assert session.goal.evaluator.model == "judge-model"
    assert session.goal.block_cap == 3
    assert session.max_turns == 5
    monkeypatch.delenv("GOAL_EVALUATOR_MODEL_ID")
    assert s17.make_live_session(tmp_path).goal.evaluator.model == "worker-model"
    monkeypatch.setattr(s17, "make_live_session", lambda _path: session)
    monkeypatch.setattr("builtins.input", Mock(return_value="q"))
    asyncio.run(s17.main([]))
    asyncio.run(s17.main(["/goal"]))
    client.chat.completions.create.assert_not_called()
