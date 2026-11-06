"""验证整合后的机制交互和 OpenAI 边界，不调用真实模型。"""

import json
import os
import shlex
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from openai import BadRequestError, InternalServerError, OpenAI, RateLimitError
from openai.types.chat import ChatCompletion

from experiments import s15_integrated_harness as s15


MEMORY_REQUEST = s15.MEMORY_RUNTIME.request_json_array


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(s15, "WORKDIR", tmp_path)
    for name, directory in (("TASKS", ".tasks"), ("WORKTREES", ".worktrees"),
                            ("MAILBOX", ".mailboxes")):
        monkeypatch.setattr(s15, f"{name}_DIR", tmp_path / directory)
        monkeypatch.setattr(s15, f"{name}_ROOT", tmp_path / directory)
    monkeypatch.setattr(s15, "TASK_LOCK_PATH", s15.TASKS_DIR / ".lock")
    monkeypatch.setattr(s15, "DURABLE_PATH", tmp_path / ".scheduled_tasks.json")
    monkeypatch.setattr(s15, "TRANSCRIPT_DIR", tmp_path / ".transcripts")
    monkeypatch.setattr(s15, "TOOL_RESULTS_DIR", tmp_path / ".task_outputs" / "tool-results")
    for name in ("teammate_assignments", "assignment_versions", "active_teammates",
                 "plan_gates", "plan_request_ids", "pending_requests", "mcp_clients",
                 "mcp_tool_policies", "scheduled_jobs", "background_tasks", "background_results"):
        monkeypatch.setattr(s15, name, {})
    monkeypatch.setattr(s15, "cron_queue", [])
    monkeypatch.setattr(s15, "CURRENT_TODOS", [])
    monkeypatch.setattr(s15, "rounds_since_todo", 0)
    monkeypatch.setattr(s15, "BUS", s15.MessageBus())
    monkeypatch.setattr(s15, "MODEL", "test")
    monkeypatch.setattr(s15, "PRIMARY_MODEL", "test")
    monkeypatch.setattr(s15, "FALLBACK_MODEL", None)
    monkeypatch.setattr(s15, "IDLE_SCAN_INTERVAL", 0.01)
    memory = s15.MEMORY_RUNTIME
    monkeypatch.setattr(memory, "WORKDIR", tmp_path)
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / ".memory")
    monkeypatch.setattr(memory, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    monkeypatch.setattr(memory, "request_json_array", Mock(return_value=[]))
    threads = []
    start = threading.Thread.start

    def tracked_start(thread):
        threads.append(thread)
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", tracked_start)
    yield threads
    for name in list(s15.active_teammates):
        s15.run_request_shutdown(name)
    s15._stop_all_shell_processes()
    for thread in threads:
        if thread.ident is not None:
            thread.join(3)
            assert not thread.is_alive()


def tool(call_id, tool_name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": tool_name, "arguments": json.dumps(arguments),
    }}


def completion(calls=None, content="done", reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": content if not calls else None}}],
    })


def set_client(monkeypatch, responses):
    responses = iter(responses)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    client = Mock()
    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(s15, "client", client)
    return requests


def assert_tool_pairs(messages):
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


def test_real_sdk_transport_handles_dynamic_tools_and_memory(monkeypatch):
    requests = []
    replies = iter([
        completion([tool("connect", "connect_mcp", **{"name": "docs"})]),
        completion([tool("version", "mcp__docs__get_version")]),
        completion(content="Docs API v2.1.0"), completion(content="[]"),
    ])

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert_tool_pairs(payload["messages"])
        return httpx2.Response(200, json=next(replies).model_dump(exclude_none=True))

    monkeypatch.setattr(s15.MEMORY_RUNTIME, "request_json_array", MEMORY_REQUEST)
    transport = httpx2.MockTransport(handle)
    with OpenAI(api_key="test-key", max_retries=0,
                http_client=httpx2.Client(transport=transport)) as client:
        monkeypatch.setattr(s15, "client", client)
        messages = [{"role": "user", "content": "Connect docs and report its API version"}]
        s15.agent_loop(messages, {}, messages[0]["content"])
    assert len(requests[0]["tools"]) == 26
    assert len(requests[1]["tools"]) == 28
    assert requests[2]["messages"][-1] == {
        "role": "tool", "tool_call_id": "version", "content": "[docs] API v2.1.0",
    }
    assert "Extract only durable knowledge" in requests[3]["messages"][-1]["content"]
    assert s15.extract_text(messages[-1]["content"]) == "Docs API v2.1.0"


def test_skill_todo_task_graph_and_subagent_share_the_host(monkeypatch):
    (s15.WORKDIR / "input.txt").write_text("child-only file contents")
    skill_name = next(iter(s15.SKILL_REGISTRY))
    requests = []
    main_turn = child_turn = 0

    def create(**kwargs):
        nonlocal main_turn, child_turn
        requests.append(deepcopy(kwargs))
        assert_tool_pairs(kwargs["messages"])
        if "You are a coding subagent" in kwargs["messages"][0]["content"]:
            child_turn += 1
            assert "parent-private-marker" not in str(kwargs["messages"])
            if child_turn == 1:
                return completion([tool("child-read", "read_file", path="input.txt")])
            return completion(content="Child checked input.txt")
        main_turn += 1
        if main_turn == 1:
            return completion([
                tool("create-a", "create_task", subject="Inspect"),
                tool("create-b", "create_task", subject="Verify"),
                tool("skill", "load_skill", **{"name": skill_name}),
                tool("todo", "todo_write", todos=[{"content": "Inspect", "status": "in_progress"}]),
            ])
        if main_turn == 2:
            tasks = {task.subject: task.id for task in s15.list_tasks()}
            return completion([
                tool("dependency", "update_task", task_id=tasks["Verify"], addBlockedBy=[tasks["Inspect"]]),
                tool("delegate", "task", description="Read input.txt and return a summary"),
            ])
        return completion(content="Inspection arranged")

    client = Mock()
    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(s15, "client", client)
    messages = [{"role": "user", "content": "parent-private-marker: inspect this project"}]
    s15.agent_loop(messages, {}, messages[0]["content"])
    tasks = {task.subject: task for task in s15.list_tasks()}
    assert tasks["Verify"].blockedBy == [tasks["Inspect"].id]
    assert not s15.can_start(tasks["Verify"].id)
    assert s15.CURRENT_TODOS == [{"content": "Inspect", "status": "in_progress"}]
    parent_messages = s15.to_openai_messages(messages)
    result = next(m for m in parent_messages if m.get("tool_call_id") == "delegate")
    assert result["content"] == "Child checked input.txt"
    assert "child-only file contents" not in str(parent_messages)
    assert any(m.get("tool_call_id") == "skill" and m["content"] == s15.load_skill(skill_name)
               for m in parent_messages)
    assert main_turn == 3 and child_turn == 2


def test_memory_recall_and_extraction_use_existing_s09_runtime(monkeypatch):
    memory = s15.MEMORY_RUNTIME
    monkeypatch.setattr(memory, "request_json_array", MEMORY_REQUEST)
    memory.write_memory_file("Dependency manager", "project", "Use uv", "This project uses uv.")
    extracted = [{"name": "Response language", "type": "user", "scope": "persistent",
                  "description": "Chinese responses", "body": "Answer in Chinese."}]
    requests = set_client(monkeypatch, [
        completion(content="[0]"), completion(content="会使用 uv，并用中文回答。"),
        completion(content=json.dumps(extracted)),
    ])
    messages = [{"role": "user", "content": "这个项目用什么管理依赖？请记住以后用中文回答。"}]
    s15.agent_loop(messages, {}, messages[0]["content"])
    assert "This project uses uv." in requests[1]["messages"][0]["content"]
    assert {record["name"] for record in memory.list_memory_files()} == {
        "Dependency manager", "Response language",
    }
    assert "Response language" in memory.read_memory_index()


def test_background_placeholder_and_late_notification_keep_tool_pairs(monkeypatch, isolated_runtime):
    started, release = threading.Event(), threading.Event()

    def command(*args):
        started.set()
        assert release.wait(3)
        return "background complete", 0

    monkeypatch.setattr(s15, "_run_bash_process", command)
    monkeypatch.setattr(s15.CONSOLE, "ask", Mock(return_value="y"))
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        assert_tool_pairs(kwargs["messages"])
        if len(requests) == 1:
            return completion([
                tool("background", "bash", command="test command", run_in_background=True),
                tool("other", "write_file", path="other.txt", content="hello"),
            ])
        if len(requests) == 2:
            assert started.wait(3)
            assert (s15.WORKDIR / "other.txt").read_text() == "hello"
            assert "Background task" in str(kwargs["messages"])
            assert "<task_notification>" not in str(kwargs["messages"])
            release.set()
            isolated_runtime[-1].join(3)
            assert not isolated_runtime[-1].is_alive()
            return completion([tool("read", "read_file", path="other.txt")])
        return completion()

    client = Mock()
    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(s15, "client", client)
    messages = [{"role": "user", "content": "Start background work and inspect another file"}]
    try:
        s15.agent_loop(messages, {}, messages[0]["content"])
    finally:
        release.set()
    assert requests[2]["messages"][-2]["tool_call_id"] == "read"
    assert "<status>completed</status>" in requests[2]["messages"][-1]["content"]
    assert "background complete" in requests[2]["messages"][-1]["content"]
    assert not s15.background_tasks and not s15.background_results


@pytest.mark.parametrize("event", ["cron", "background", "team"])
def test_async_events_start_a_turn_without_user_input(monkeypatch, event):
    job = None
    if event == "cron":
        job = s15.schedule_job("* * * * *", "Check time", recurring=False, durable=False)
        s15._enqueue_due_job(job)
    elif event == "background":
        s15.background_tasks["bg_test"] = {"status": "completed", "command": "test"}
        s15.background_results["bg_test"] = "finished"
    else:
        s15.BUS.send("alice", "lead", "Investigation finished", "result")
    requests = set_client(monkeypatch, [completion()])
    monkeypatch.setattr(s15.time, "sleep", Mock(side_effect=[None, StopIteration]))
    with pytest.raises(StopIteration):
        s15.async_event_loop([], {}, {"active_user_request": "Inspect project"})
    assert len(requests) == 1
    text = str(requests[0]["messages"])
    assert {"cron": "[Scheduled] Check time", "background": "<task_notification>",
            "team": "[Team events]"}[event] in text
    if job:
        assert job.id not in s15.scheduled_jobs
    assert not s15.cron_queue and not s15.has_pending_background() and not s15.BUS.peek("lead")


def api_error(status, code):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.invalid/v1"))
    cls = RateLimitError if status == 429 else InternalServerError if status >= 500 else BadRequestError
    return cls(code, response=response, body={"code": code})


@pytest.mark.parametrize("status", [429, 503])
def test_retry_uses_openai_status_and_only_configured_fallback(monkeypatch, status):
    monkeypatch.setattr(s15, "FALLBACK_MODEL", "backup")
    requests = set_client(monkeypatch, [api_error(status, "temporary"),
                                       api_error(status, "temporary"), completion()])
    sleep = Mock()
    monkeypatch.setattr(s15.time, "sleep", sleep)
    s15.agent_loop([{"role": "user", "content": "Work"}], {}, "Work")
    assert [request["model"] for request in requests] == [
        "test", "test", "backup" if status == 503 else "test",
    ]
    assert sleep.call_count == 2


def test_truncated_json_calls_escalate_then_continue_without_execution(monkeypatch):
    truncated = completion([tool("partial", "write_file", path="a.txt")], reason="length")
    truncated.choices[0].message.tool_calls[0].function.arguments = '{"path":'
    requests = set_client(monkeypatch, [truncated, truncated, completion()])
    messages = [{"role": "user", "content": "Write a file"}]
    s15.agent_loop(messages, {}, messages[0]["content"])
    assert [request["max_completion_tokens"] for request in requests] == [8000, 16000, 16000]
    assert requests[2]["messages"][-1]["content"] == s15.CONTINUATION_PROMPT
    assert not (s15.WORKDIR / "a.txt").exists()
    for request in requests:
        assert_tool_pairs(request["messages"])
        assert all(message["role"] != "assistant" or message.get("content") or message.get("tool_calls")
                   for message in request["messages"])


def test_reactive_compact_preserves_pairs_and_authoritative_request(monkeypatch):
    messages = [{"role": "user", "content": "Original context"}]
    for index in range(5):
        messages.extend([
            {"role": "assistant", "content": [SimpleNamespace(
                type="tool_use", id=f"read-{index}", name="read_file", input={"path": "a.txt"})]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"read-{index}",
                                            "content": "Read file contents"}]},
        ])
    messages.append({"role": "user", "content": "Only inspect, do not change files"})
    requests = set_client(monkeypatch, [
        api_error(400, "context_length_exceeded"), completion(content="Files inspected."), completion(),
    ])
    s15.agent_loop(messages, {}, "Only inspect, do not change files")
    assert "Create a compact factual state summary" in requests[1]["messages"][0]["content"]
    assert "Authoritative request:\nOnly inspect, do not change files" in requests[2]["messages"][1]["content"]
    for request in requests:
        assert_tool_pairs(request["messages"])
    assert list(s15.TRANSCRIPT_DIR.glob("*.jsonl"))


def test_explicit_compact_finishes_the_tool_batch_before_summarizing(monkeypatch):
    requests = set_client(monkeypatch, [completion([
        tool("compact", "compact"), tool("write", "write_file", path="a.txt", content="hello"),
    ]), completion(content="a.txt written"), completion()])
    messages = [{"role": "user", "content": "Write a.txt, compact, then report"}]
    s15.agent_loop(messages, {}, messages[0]["content"])
    assert (s15.WORKDIR / "a.txt").read_text() == "hello"
    assert '"tool_use_id": "compact"' in requests[1]["messages"][-1]["content"]
    assert '"tool_use_id": "write"' in requests[1]["messages"][-1]["content"]
    assert "[Compacted]" in requests[2]["messages"][1]["content"]
    assert_tool_pairs(requests[2]["messages"])


@pytest.mark.parametrize("recurring", [False, True])
def test_s12_schedule_records_restore_and_failed_delivery_is_requeued(monkeypatch, recurring):
    record = {"id": "cron_1234abcd", "cron": "* * * * *", "prompt": "Reminder",
              "recurring": recurring, "durable": True, "pending_delivery": True,
              "last_fired": "2026-11-05 09:00"}
    s15.DURABLE_PATH.write_text(json.dumps([record]))
    s15.load_durable_jobs()
    job = s15.scheduled_jobs[record["id"]]
    assert job.last_fired == record["last_fired"] and s15.cron_queue == [job]
    set_client(monkeypatch, [api_error(400, "invalid request")])
    messages = [{"role": "user", "content": "Work"}]
    s15.agent_loop(messages, {}, "Work")
    assert s15.cron_queue == [job] and job.pending_delivery
    assert messages[-1]["content"][0]["text"].startswith("[Error] BadRequestError:")
    set_client(monkeypatch, [completion()])
    s15.agent_loop(messages, {}, "Work")
    assert not s15.cron_queue
    saved = json.loads(s15.DURABLE_PATH.read_text())
    if recurring:
        assert s15.scheduled_jobs[job.id] is job and not job.pending_delivery
        assert len(saved) == 1 and saved[0]["pending_delivery"] is False
    else:
        assert not s15.scheduled_jobs and saved == []


def test_corrupt_schedule_store_is_not_silently_replaced():
    s15.DURABLE_PATH.write_text("broken JSON")
    with pytest.raises(json.JSONDecodeError):
        s15.load_durable_jobs()
    assert s15.DURABLE_PATH.read_text() == "broken JSON"
    assert not s15.scheduled_jobs


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd or s15.WORKDIR,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_team_plan_gate_worktree_and_shutdown_use_the_integrated_host(monkeypatch, isolated_runtime):
    git("init", "-q")
    (s15.WORKDIR / "base.txt").write_text("base")
    git("add", "base.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    task = s15.create_task("Write result.txt", "Only modify result.txt in the assigned worktree")
    assert "created at" in s15.create_worktree("isolated", task.id)
    waiting, idle = threading.Event(), threading.Event()
    requests = []
    send = s15.BUS.send

    def observed_send(*args, **kwargs):
        send(*args, **kwargs)
        if len(args) > 3 and args[3] == "idle_notification":
            idle.set()

    monkeypatch.setattr(s15.BUS, "send", observed_send)

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        assert_tool_pairs(kwargs["messages"])
        if len(requests) == 1:
            return completion([
                tool("early-write", "write_file", path="result.txt", content="too early"),
                tool("plan", "submit_plan", plan="Only write result.txt and verify its contents"),
            ])
        if len(requests) == 2:
            waiting.set()
            return completion(content="Waiting for approval")
        if len(requests) == 3:
            return completion([
                tool("write", "write_file", path="result.txt", content="approved"),
                tool("complete", "complete_task", task_id=task.id),
            ])
        return completion(content="Result written")

    client = Mock()
    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(s15, "client", client)
    assert "spawned" in s15.spawn_teammate_thread(
        "alice", "developer", "Work only on your assigned file", task.id, require_plan=True,
    )
    assert waiting.wait(3)
    assert requests[1]["messages"][-2]["content"] == "Blocked: plan status is required."
    assert not (s15.WORKTREES_DIR / "isolated" / "result.txt").exists()
    plan = s15.consume_lead_inbox()[0]
    s15.run_review_plan(plan["metadata"]["request_id"], True)
    assert idle.wait(3)
    assert (s15.WORKTREES_DIR / "isolated" / "result.txt").read_text() == "approved"
    assert not (s15.WORKDIR / "result.txt").exists()
    assert s15.load_task(task.id).status == "completed"
    assert "alice" not in s15.teammate_assignments
    assert [event["type"] for event in s15.consume_lead_inbox()] == ["result", "idle_notification"]
    s15.run_request_shutdown("alice")
    isolated_runtime[-1].join(3)
    assert not isolated_runtime[-1].is_alive()
    assert s15.consume_lead_inbox()[0]["type"] == "shutdown_response"
    assert not s15.active_teammates


def test_async_shell_approval_is_denied_without_reading_stdin(monkeypatch):
    ask = Mock(side_effect=AssertionError("must not read stdin"))
    monkeypatch.setattr(s15.CONSOLE, "ask", ask)
    outputs = []
    worker = threading.Thread(target=lambda: outputs.append(s15.permission_hook(
        SimpleNamespace(name="bash", input={"command": "echo test"})
    )))
    worker.start()
    worker.join(3)
    assert "interactive shell approval is unavailable" in outputs[0]
    ask.assert_not_called()


def test_shell_execution_reports_exit_status_and_cleans_process_group():
    args = [sys.executable, "-c", "import sys; print('checked'); sys.exit(7)"]
    command = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    output = s15.run_bash(command)
    assert "status 7" in output and "checked" in output
    assert not s15._shell_processes


def test_import_and_cli_quit_need_no_model(tmp_path):
    env = dict(os.environ, OPENAI_API_KEY="test-key", MODEL_ID="test-model",
               OPENAI_BASE_URL="https://example.invalid/v1")
    script = Path(s15.__file__).resolve()
    code = ("import threading, signal; previous=signal.getsignal(signal.SIGTERM); "
            "from experiments import s15_integrated_harness; "
            "assert len(threading.enumerate()) == 1; "
            "assert signal.getsignal(signal.SIGTERM) == previous")
    for args in (["-c", code], [str(script)]):
        result = subprocess.run([sys.executable, *args], input="q\n", text=True,
                                capture_output=True, env=env, timeout=15,
                                cwd=script.parents[1] if args[0] == "-c" else tmp_path)
        assert result.returncode == 0, result.stderr
        assert not result.stderr
