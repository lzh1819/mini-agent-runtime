"""验证时间匹配、持久交付和空闲调度，不请求外部 API。"""

import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s12_cron_scheduler as s12


NOW = datetime(2026, 11, 5, 9, 0)  # Thursday


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(s12, "WORKDIR", tmp_path)
    monkeypatch.setattr(s12, "DURABLE_PATH", tmp_path / ".scheduled_tasks.json")
    monkeypatch.setattr(s12, "scheduled_jobs", {})
    monkeypatch.setattr(s12, "cron_queue", [])
    monkeypatch.setattr(s12, "agent_lock", threading.Lock())


def completion(calls=None, reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else "done"}}],
    })


def tool(name, **arguments):
    return {"id": "call-1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


@pytest.mark.parametrize("cron,matches", [
    ("* * * * *", True),
    ("*/5 9 * 11 1-5", True),
    ("1,15,30 9 * * *", False),
    ("*/5,7 9 * * *", True),
    ("0 9 6 * 4", True),  # day OR weekday
    ("0 9 5 * 1", True),
    ("0 9 6 * 1", False),
    ("0 10 * * *", False),
    ("0 9 * 12 *", False),
    ("0 9 * * 0", False),
])
def test_cron_matches(cron, matches):
    assert s12.validate_cron(cron) is None
    assert s12.cron_matches(cron, NOW) is matches
    assert s12.cron_matches("0 9 * * 0", datetime(2026, 11, 8, 9))


@pytest.mark.parametrize("cron", [
    "* * * *", "60 * * * *", "0 24 * * *", "0 9 0 * *", "0 9 * 13 *",
    "0 9 * * 7", "*/0 * * * *", "9-3 * * * *", "a * * * *", "1, * * * *",
])
def test_invalid_cron_is_not_saved(cron):
    assert s12.run_schedule_cron(cron, "work").startswith("Error:")
    assert not s12.scheduled_jobs and not s12.DURABLE_PATH.exists()


def test_due_deduplication_restart_acknowledgement_and_cancel():
    recurring = s12.schedule_job("* * * * *", "repeat")
    once = s12.schedule_job("* * * * *", "once", recurring=False)
    session = s12.schedule_job("* * * * *", "temporary", durable=False)
    s12.poll_due_jobs(NOW)
    s12.poll_due_jobs(NOW)
    s12.poll_due_jobs(NOW + timedelta(minutes=1))
    assert len(s12.cron_queue) == 3  # still pending, even in the next minute
    assert "[one-shot, durable]" in s12.run_list_crons()
    assert "[recurring, session]" in s12.run_list_crons()

    s12.scheduled_jobs.clear()
    s12.cron_queue.clear()
    s12.load_durable_jobs()
    assert set(s12.scheduled_jobs) == {recurring.id, once.id}
    assert session.id not in s12.scheduled_jobs
    fired = s12.consume_cron_queue()
    assert {job.id for job in fired} == {recurring.id, once.id}
    s12.acknowledge_cron_jobs(fired)
    assert once.id not in s12.scheduled_jobs
    assert not s12.scheduled_jobs[recurring.id].pending_delivery
    s12.poll_due_jobs(NOW)  # acknowledgement does not allow the same minute again
    assert not s12.has_cron_queue()
    s12.poll_due_jobs(NOW + timedelta(minutes=1))
    assert len(s12.cron_queue) == 1
    assert s12.run_cancel_cron(recurring.id) == f"Cancelled {recurring.id}"
    assert not s12.has_cron_queue()
    assert json.loads(s12.DURABLE_PATH.read_text()) == []


@pytest.mark.parametrize("failure", ["request", "finish", "ack"])
def test_failed_delivery_restores_queue_and_history(monkeypatch, failure):
    job = s12.schedule_job("* * * * *", "work", recurring=False)
    s12.poll_due_jobs(NOW)
    messages = [{"role": "system", "content": "test"}]
    before = deepcopy(messages)
    client = Mock()
    client.chat.completions.create.return_value = completion(
        [tool("write_file", path="work.txt", content="hello")],
        reason="length" if failure == "finish" else None,
    )
    if failure == "request":
        client.chat.completions.create.side_effect = RuntimeError("request failed")
    if failure == "ack":
        monkeypatch.setattr(s12, "save_durable_jobs", Mock(side_effect=OSError("disk failed")))
    with pytest.raises((RuntimeError, OSError)):
        s12.agent_loop(messages, client, "test")
    assert messages == before
    assert s12.cron_queue == [job] and job.pending_delivery
    assert s12.scheduled_jobs[job.id] is job
    assert not (s12.WORKDIR / "work.txt").exists()


def test_scheduled_prompt_uses_normal_tool_loop():
    job = s12.schedule_job("* * * * *", "write hello", recurring=False)
    s12.poll_due_jobs(NOW)
    client = Mock()
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        if len(requests) == 1:
            return completion([tool("write_file", path="hello.txt", content="hello")])
        assert job.id not in s12.scheduled_jobs  # acknowledged on the first response
        return completion()

    client.chat.completions.create.side_effect = create
    messages = [{"role": "system", "content": "test"}]
    s12.agent_loop(messages, client, "test")
    assert requests[0][-1] == {"role": "user", "content": "[Scheduled] write hello"}
    assert requests[1][-1]["role"] == "tool"
    assert requests[1][-1]["tool_call_id"] == "call-1"
    assert (s12.WORKDIR / "hello.txt").read_text() == "hello"
    assert not s12.cron_queue


@pytest.mark.parametrize("operation", ["schedule", "enqueue", "cancel"])
def test_atomic_save_failure_rolls_back_state(monkeypatch, operation):
    job = s12.schedule_job("* * * * *", "work")
    if operation == "cancel":
        s12.poll_due_jobs(NOW)
    saved = s12.DURABLE_PATH.read_bytes()
    queue = list(s12.cron_queue)
    old_state = (job.pending_delivery, job.last_fired)
    monkeypatch.setattr(s12.os, "replace", Mock(side_effect=OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        if operation == "schedule":
            s12.schedule_job("* * * * *", "another")
        elif operation == "enqueue":
            s12.poll_due_jobs(NOW)
        else:
            s12.cancel_job(job.id)
    assert s12.DURABLE_PATH.read_bytes() == saved
    assert s12.scheduled_jobs == {job.id: job}
    assert s12.cron_queue == queue
    assert (job.pending_delivery, job.last_fired) == old_state
    assert not list(s12.WORKDIR.glob("*.tmp"))


def test_corrupt_storage_fails_without_overwriting_it():
    s12.DURABLE_PATH.write_text("broken json")
    with pytest.raises(json.JSONDecodeError):
        s12.load_durable_jobs()
    assert s12.DURABLE_PATH.read_text() == "broken json"
    assert not s12.scheduled_jobs


def test_queue_waits_for_idle_then_wakes_agent_without_user(monkeypatch):
    s12.schedule_job("* * * * *", "work", durable=False)
    s12.poll_due_jobs(NOW)
    stop = threading.Event()
    checked = threading.Event()
    delivered = threading.Event()
    has_queue = s12.has_cron_queue

    def observed_queue():
        checked.set()
        return has_queue()

    def create(**kwargs):
        assert kwargs["messages"][-1]["content"] == "[Scheduled] work"
        delivered.set()
        stop.set()
        return completion()

    monkeypatch.setattr(s12, "has_cron_queue", observed_queue)
    client = Mock()
    client.chat.completions.create.side_effect = create
    worker = threading.Thread(target=s12.queue_processor_loop,
                              args=([], client, "test", stop))
    try:
        with s12.agent_lock:
            worker.start()
            assert checked.wait(3)
            client.chat.completions.create.assert_not_called()
        assert delivered.wait(3)
    finally:
        stop.set()
        worker.join(3)
    assert not worker.is_alive() and not s12.agent_lock.locked()
    assert client.chat.completions.create.call_count == 1


def test_scheduled_turn_cannot_read_interactive_approval(monkeypatch, capsys):
    read_input = Mock(side_effect=AssertionError("must not read terminal"))
    monkeypatch.setattr("builtins.input", read_input)
    decisions = []
    worker = threading.Thread(target=lambda: decisions.append(
        s12.check_permission("bash", {"command": "rm example.txt"})
    ))
    worker.start()
    worker.join(3)
    assert decisions == [False]
    read_input.assert_not_called()
    assert "scheduled turns cannot request interactive approval" in capsys.readouterr().out


def test_cli_import_has_no_threads_and_quit_needs_no_model(tmp_path):
    env = dict(os.environ, OPENAI_API_KEY="test-key", MODEL_ID="test-model",
               OPENAI_BASE_URL="https://example.invalid/v1")
    script = Path(s12.__file__).resolve()
    for args in (["-c", "import threading; from experiments import s12_cron_scheduler; "
                  "assert len(threading.enumerate()) == 1"], [str(script)]):
        result = subprocess.run([sys.executable, *args], input="q\n", text=True,
                                capture_output=True, env=env, timeout=15,
                                cwd=script.parents[1] if args[0] == "-c" else tmp_path)
        assert result.returncode == 0, result.stderr
        assert not result.stderr
