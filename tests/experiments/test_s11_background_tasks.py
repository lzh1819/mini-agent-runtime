"""验证后台交错执行、通知投递、权限与进程清理，不请求外部 API。"""

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s11_background_tasks as s11


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


def background_call(command="work"):
    return completion([tool("bg-call", "bash", command=command, run_in_background=True)]).choices[0].message.tool_calls[0]


def configure_background(tmp_path, monkeypatch):
    manager = s11.BackgroundManager()
    monkeypatch.setattr(s11, "BACKGROUND", manager)
    monkeypatch.setattr(s11, "WORKDIR", tmp_path)
    return manager


def watch_worker(manager, monkeypatch):
    finished = threading.Event()
    run = manager._run

    def observed_run(*args):
        try:
            run(*args)
        finally:
            finished.set()

    monkeypatch.setattr(manager, "_run", observed_run)
    return finished


def python_command(code):
    args = [sys.executable, "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def test_loop_continues_while_worker_runs_and_delivers_one_notification(tmp_path, monkeypatch):
    manager = configure_background(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    finished = watch_worker(manager, monkeypatch)
    post_calls = []
    monkeypatch.setitem(s11.HOOKS, "PostToolUse", [
        lambda name, args, output: post_calls.append((name, output)),
    ])

    def run(command):
        started.set()
        assert release.wait(3)
        return "background output", 0

    monkeypatch.setattr(s11, "_run_bash_process", run)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            return completion([
                tool("bg", "bash", command="independent work", run_in_background=True),
                tool("write", "write_file", path="other.txt", content="hello"),
            ])
        if len(requests) == 2:
            assert started.wait(1)
            assert (tmp_path / "other.txt").read_text() == "hello"
            assert manager.tasks["bg_0001"]["status"] == "running"
            assert "<task_notification>" not in str(kwargs["messages"])
            release.set()
            assert finished.wait(3)
            return completion([tool("read", "read_file", path="other.txt")])
        return completion()

    client = Mock()
    client.chat.completions.create.side_effect = create
    messages = [{"role": "user", "content": "Run work in the background and inspect another file."}]
    try:
        s11.agent_loop(messages, client, "test")
    finally:
        release.set()
        assert finished.wait(3)

    original_results = [message for message in messages if message.get("tool_call_id") == "bg"]
    assert len(original_results) == 1
    assert original_results[0]["content"].startswith("[Background task bg_0001 started]")
    notification = requests[2]["messages"][-1]
    assert notification["role"] == "user" and "tool_call_id" not in notification
    assert "<status>completed</status>" in notification["content"]
    assert "background output" in notification["content"]
    assert requests[2]["messages"][-2] == {"role": "tool", "tool_call_id": "read", "content": "hello"}
    assert [name for name, _ in post_calls] == ["bash", "write_file", "read_file"]
    assert post_calls[0][1].startswith("[Background task")
    assert manager.collect() == [] and manager.tasks == {} and manager.results == {}


def test_late_completion_is_collected_on_next_user_turn(tmp_path, monkeypatch):
    manager = configure_background(tmp_path, monkeypatch)
    release = threading.Event()
    finished = watch_worker(manager, monkeypatch)

    def run(command):
        assert release.wait(3)
        return "late result", 0

    monkeypatch.setattr(s11, "_run_bash_process", run)
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion([tool("bg", "bash", command="work", run_in_background=True)]), completion(),
    ]
    messages = [{"role": "user", "content": "Start the background command."}]
    try:
        s11.agent_loop(messages, client, "test")
        assert manager.tasks["bg_0001"]["status"] == "running"
    finally:
        release.set()
        assert finished.wait(3)
    assert client.chat.completions.create.call_count == 2
    assert all("<task_notification>" not in (message.get("content") or "") for message in messages)

    query = {"role": "user", "content": "Check the result now."}
    messages.append(query)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return completion()

    client.chat.completions.create.side_effect = create
    s11.agent_loop(messages, client, "test")
    assert requests[0][-2] == query
    assert "late result" in requests[0][-1]["content"]
    assert manager.collect() == []


@pytest.mark.parametrize("outcome,expected", [
    (("command failed", 7), "status 7"),
    (("Error: Timeout (120s)", None), "Timeout"),
    (RuntimeError("worker exploded"), "RuntimeError: worker exploded"),
])
def test_worker_failures_are_failed_notifications(tmp_path, monkeypatch, outcome, expected):
    manager = configure_background(tmp_path, monkeypatch)
    finished = watch_worker(manager, monkeypatch)
    run = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    monkeypatch.setattr(s11, "_run_bash_process", run)
    call = background_call()

    manager.start(call, json.loads(call.function.arguments))
    assert finished.wait(3)
    notifications = manager.collect()

    assert len(notifications) == 1
    assert "<status>failed</status>" in notifications[0]
    assert expected in notifications[0]
    assert manager.collect() == [] and manager.tasks == {} and manager.results == {}


@pytest.mark.parametrize("command,asks", [("sudo echo test", 0), ("rm example.txt", 1)])
def test_permission_is_checked_before_starting_worker(tmp_path, monkeypatch, command, asks):
    manager = configure_background(tmp_path, monkeypatch)
    start = Mock()
    after = Mock()
    ask = Mock(return_value="deny")
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(s11, "ask_user", ask)
    monkeypatch.setitem(s11.HOOKS, "PostToolUse", [after])

    assert s11.execute_tool(background_call(command)) == "Permission denied."
    start.assert_not_called()
    after.assert_not_called()
    assert ask.call_count == asks


def test_thread_start_failure_removes_registered_task(tmp_path, monkeypatch):
    manager = configure_background(tmp_path, monkeypatch)
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("thread unavailable")))
    call = background_call()

    with pytest.raises(RuntimeError, match="thread unavailable"):
        manager.start(call, json.loads(call.function.arguments))
    assert manager.tasks == {} and manager.results == {} and manager.collect() == []


@pytest.mark.parametrize("flag", [None, False])
def test_foreground_bash_reports_exit_code_and_stays_synchronous(tmp_path, monkeypatch, flag):
    manager = configure_background(tmp_path, monkeypatch)
    arguments = {"command": python_command(
        "import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit(7)"
    )}
    if flag is not None:
        arguments["run_in_background"] = flag
    call = completion([tool("foreground", "bash", **arguments)]).choices[0].message.tool_calls[0]

    output = s11.execute_tool(call)

    assert output.startswith("Error: command exited with status 7")
    assert "stdout" in output and "stderr" in output
    assert manager.tasks == {} and not s11._shell_processes


@pytest.mark.skipif(sys.platform != "linux", reason="Uses /proc to distinguish stopped children from zombies")
def test_timeout_stops_children_even_when_they_ignore_sigterm(tmp_path, monkeypatch):
    configure_background(tmp_path, monkeypatch)
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"p = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE, text=True); "
        "assert p.stdout.readline().strip() == 'ready'; print(p.pid, flush=True); time.sleep(30)"
    )
    children = []

    def timeout_after_child_is_ready(process, timeout):
        children.append(int(process.stdout.readline().strip()))
        raise subprocess.TimeoutExpired(process.args, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", timeout_after_child_is_ready)
    try:
        output, exit_code = s11._run_bash_process(python_command(parent_code))
        assert "Timeout" in output and exit_code is None
        assert not s11._shell_processes
        child = Path(f"/proc/{children[0]}/stat")
        assert not child.exists() or child.read_text().split()[2] == "Z"
    finally:
        for child in children:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_shutdown_stops_running_process_and_prevents_late_start(tmp_path, monkeypatch):
    manager = configure_background(tmp_path, monkeypatch)
    monkeypatch.setattr(s11, "_shell_stopping", False)
    started = threading.Event()
    finished = watch_worker(manager, monkeypatch)
    communicate = subprocess.Popen.communicate

    def observed_communicate(process, **kwargs):
        started.set()
        return communicate(process, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", observed_communicate)
    call = background_call(python_command("import time; time.sleep(30)"))
    manager.start(call, json.loads(call.function.arguments))
    try:
        assert started.wait(3)
        s11._stop_all_shell_processes()
        assert finished.wait(3)
        assert not s11._shell_processes
        assert "<status>failed</status>" in manager.collect()[0]
        popen = Mock()
        monkeypatch.setattr(subprocess, "Popen", popen)
        assert s11._run_bash_process("must not start") == ("Error: Shell runtime is shutting down", None)
        popen.assert_not_called()
    finally:
        s11._stop_all_shell_processes()
        assert finished.wait(3)
