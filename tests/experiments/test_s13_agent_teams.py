"""用假模型验证持久队友、任务认领和团队协议，不请求外部 API。"""

import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s13_agent_teams as s13


@pytest.fixture(autouse=True)
def isolated_team(tmp_path, monkeypatch):
    monkeypatch.setattr(s13, "WORKDIR", tmp_path)
    for name, directory in (("TASKS", ".tasks"), ("WORKTREES", ".worktrees"),
                            ("MAILBOX", ".mailboxes")):
        monkeypatch.setattr(s13, f"{name}_DIR", tmp_path / directory)
        monkeypatch.setattr(s13, f"{name}_ROOT", tmp_path / directory)
    monkeypatch.setattr(s13, "TASK_LOCK_PATH", s13.TASKS_DIR / ".lock")
    for name in ("teammate_assignments", "assignment_versions", "active_teammates",
                 "plan_gates", "plan_request_ids", "pending_requests", "teammate_threads"):
        monkeypatch.setattr(s13, name, {})
    monkeypatch.setattr(s13, "BUS", s13.MessageBus())
    monkeypatch.setattr(s13, "TEAM_STOP", threading.Event())
    monkeypatch.setattr(s13, "IDLE_SCAN_INTERVAL", 0.01)
    yield
    s13.TEAM_STOP.set()
    for thread in list(s13.teammate_threads.values()):
        thread.join(3)
        assert not thread.is_alive()


def completion(calls=None, content="done", reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else content}}],
    })


def tool(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def call(name, **arguments):
    return completion([tool("call", name, **arguments)]).choices[0].message.tool_calls[0]


def runtime_for(task, *, name="alice", require_plan=False, client=None):
    s13.active_teammates[name] = "working"
    s13.plan_gates[name] = "required" if require_plan else "not_required"
    assert s13.claim_task(task.id, name).startswith("Claimed ")
    return s13.TeammateRuntime(name, "developer", "Work on the assigned file.",
                               task.id, require_plan, client or Mock(), "test")


def test_persistent_teammate_auto_claims_then_shutdown_wakes_lead(monkeypatch):
    first = s13.create_task("Write first.txt", "Only modify first.txt.")
    idle = threading.Event()
    send = s13.BUS.send

    def observed_send(*args, **kwargs):
        send(*args, **kwargs)
        if len(args) > 3 and args[3] == "idle_notification":
            idle.set()

    monkeypatch.setattr(s13.BUS, "send", observed_send)
    requests = []
    second = None

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) in (1, 3):
            task = first if len(requests) == 1 else second
            filename = "first.txt" if task is first else "second.txt"
            return completion([
                tool("write", "write_file", path=filename, content="hello"),
                tool("complete", "complete_task", task_id=task.id),
            ])
        return completion(content="Assigned file written and checked.")

    client = Mock()
    client.chat.completions.create.side_effect = create
    result = s13.spawn_teammate_thread("alice", "developer", "Only work on assigned files.",
                                       first.id, client=client, model="test")
    assert "spawned" in result
    thread = s13.teammate_threads["alice"]
    try:
        assert idle.wait(3)
        assert s13.load_task(first.id).status == "completed"
        assert "alice" not in s13.teammate_assignments
        assert thread.is_alive()
        idle.clear()
        second = s13.create_task("Write second.txt", "Only modify second.txt.")
        assert idle.wait(3)
        assert s13.load_task(second.id).status == "completed"
        assert len(requests) == 4
        assert requests[0]["messages"][0]["role"] == "system"
        assert "Only work on assigned files." in requests[0]["messages"][1]["content"]
        assert any("Auto-claimed task" in (m.get("content") or "")
                   for m in requests[2]["messages"])
        assert requests[2]["messages"][1] == requests[0]["messages"][1]
        assert [m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"] == [
            "write", "complete",
        ]
        teammate_tools = {t["function"]["name"] for t in requests[0]["tools"]}
        assert not teammate_tools & {"spawn_teammate", "update_task", "create_worktree", "check_inbox"}
        assert (s13.WORKDIR / "first.txt").read_text() == "hello"
        assert (s13.WORKDIR / "second.txt").read_text() == "hello"

        assert s13.wait_for_cli_event() == ("wake", None)
        events = s13.consume_lead_inbox()
        assert [event["type"] for event in events] == [
            "result", "idle_notification", "result", "idle_notification",
        ]
        assert not s13.BUS.read_inbox("lead")
        history = [{"role": "system", "content": "Lead-only context"},
                   {"role": "user", "content": s13.format_team_events(events)}]
        lead_client = Mock()
        lead_client.chat.completions.create.return_value = completion(content="Both files checked.")
        s13.agent_loop(history, lead_client, "test")
        assert "[Team events]" in history[1]["content"]
        assert all("Lead-only context" not in str(r["messages"]) for r in requests)

        assert "Shutdown requested" in s13.run_request_shutdown("alice")
        thread.join(3)
        assert not thread.is_alive()
        events = s13.consume_lead_inbox()
        assert [event["type"] for event in events] == ["shutdown_response"]
        assert next(iter(s13.pending_requests.values())).status == "approved"
        assert not s13.active_teammates and not s13.teammate_threads
    finally:
        s13.TEAM_STOP.set()
        thread.join(3)


def test_two_teammates_can_call_models_concurrently():
    barrier = threading.Barrier(2)
    finished = threading.Event()
    tasks = {name: s13.create_task(f"Inspect {name}") for name in ("alice", "bob")}
    calls = []
    lock = threading.Lock()

    def create(**kwargs):
        name = "alice" if "'alice'" in kwargs["messages"][0]["content"] else "bob"
        if len(kwargs["messages"]) == 2:
            barrier.wait(3)  # neither call can finish unless both teammates run
            return completion([tool("finish", "complete_task", task_id=tasks[name].id)])
        with lock:
            calls.append(name)
            if len(calls) == 2:
                finished.set()
        return completion(content=f"{name} inspected")

    client = Mock()
    client.chat.completions.create.side_effect = create
    for name, task in tasks.items():
        s13.spawn_teammate_thread(name, "reviewer", "Inspect your assigned module.",
                                  task.id, client=client, model="test")
    assert finished.wait(3)
    assert set(calls) == {"alice", "bob"}
    assert all(s13.load_task(t.id).status == "completed" for t in tasks.values())


def test_ready_task_claim_is_atomic_and_dependencies_are_enforced():
    first = s13.create_task("First")
    second = s13.create_task("Second")
    s13.update_task(second.id, [first.id])
    with pytest.raises(ValueError, match="cycle"):
        s13.update_task(first.id, [second.id])
    assert [task.id for task in s13.scan_unclaimed_tasks()] == [first.id]
    barrier = threading.Barrier(2)
    results = []

    def claim(name):
        barrier.wait(3)
        results.append(s13.claim_task(first.id, name))

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert sum(result.startswith("Claimed ") for result in results) == 1
    owner = s13.load_task(first.id).owner
    assert "cannot complete" in s13.complete_task(first.id, "someone_else")
    assert s13.complete_task(first.id, owner).startswith("Completed ")
    assert s13.claim_next_task(owner) is None  # keep cwd until this work turn ends
    assert s13.release_completed_assignment(owner)
    assert s13.claim_next_task(owner).id == second.id


@pytest.mark.parametrize("approve", [True, False])
def test_plan_gate_waits_for_matching_approval_and_can_be_revised(approve):
    task = s13.create_task("Update a.txt")
    (s13.WORKDIR / "a.txt").write_text("old")
    runtime = runtime_for(task, require_plan=True)

    def execute(name, **args):
        return s13._run_teammate_tool("alice", call(name, **args), runtime.handlers)

    assert "Blocked: plan status is required" in execute("write_file", path="a.txt", content="new")
    assert execute("read_file", path="a.txt") == "old"
    assert "cannot complete" in runtime.complete(task.id)
    assert "Plan submitted" in execute("submit_plan", plan="Only update a.txt, then check it.")
    request = s13.consume_lead_inbox()[0]
    request_id = request["metadata"]["request_id"]
    assert request["type"] == "plan_approval_request"
    assert "Blocked: plan status is pending" in execute("bash", command="echo test")
    s13.run_review_plan(request_id, approve, "Reviewed")
    response = s13.BUS.read_inbox("alice")[0]
    forged = {**response, "from": "bob"}
    assert not s13.apply_plan_response("alice", forged)[0]
    assert s13.plan_gates["alice"] == "pending"
    assert runtime.handle_inbox([response]) is False
    assert s13.plan_gates["alice"] == ("approved" if approve else "rejected")
    assert not s13.apply_plan_response("alice", response)[0]  # no replay
    if not approve:
        assert execute("write_file", path="a.txt", content="new").startswith("Blocked:")
        execute("submit_plan", plan="Revised plan")
        revised = s13.consume_lead_inbox()[0]["metadata"]["request_id"]
        s13.run_review_plan(revised, True)
        runtime.handle_inbox(s13.BUS.read_inbox("alice"))
    assert execute("write_file", path="a.txt", content="new").startswith("Wrote ")
    assert runtime.complete(task.id).startswith("Completed ")
    s13.release_completed_assignment("alice")
    next_task = s13.create_task("Next work")
    assert runtime.claim(next_task.id).startswith("Claimed ")
    assert not s13.apply_plan_response("alice", response)[0]
    assert (s13.WORKDIR / "a.txt").read_text() == "new"


def test_old_plan_review_and_forged_shutdown_do_not_change_current_work():
    task = s13.create_task("Work")
    runtime = runtime_for(task, require_plan=True)
    s13._teammate_submit_plan("alice", "Plan")
    old_id = s13.plan_request_ids["alice"]
    s13.advance_assignment_version("alice")
    assert "earlier assignment" in s13.run_review_plan(old_id, True)
    s13.run_request_shutdown("alice")
    message = s13.BUS.read_inbox("alice")[0]
    assert not s13.apply_shutdown_request("alice", {**message, "from": "bob"})[0]
    request_id = message["metadata"]["request_id"]
    assert not s13.match_response("plan_approval_response", request_id, True, "alice", "lead")
    assert s13.pending_requests[request_id].status == "pending"
    assert runtime.handle_inbox([message])
    s13.consume_lead_inbox()
    assert s13.pending_requests[request_id].status == "approved"


def test_teammate_permissions_and_hooks_cannot_be_bypassed(monkeypatch):
    task = s13.create_task("Work")
    runtime = runtime_for(task)
    read_input = Mock(side_effect=AssertionError("background approval must not read stdin"))
    monkeypatch.setattr("builtins.input", read_input)
    for command in ("sudo echo test", "rm example.txt"):
        output = s13._run_teammate_tool("alice", call("bash", command=command), runtime.handlers)
        assert output.startswith("Permission ")
    output = s13._run_teammate_tool("alice", call("read_file", path="../outside.txt"), runtime.handlers)
    assert "outside the workspace" in output
    read_input.assert_not_called()
    after = Mock()
    monkeypatch.setitem(s13.HOOKS, "PreToolUse", [s13.permission_hook, lambda *args: "Blocked by hook"])
    monkeypatch.setitem(s13.HOOKS, "PostToolUse", [after])
    assert s13._run_teammate_tool("alice", call("read_file", path="missing.txt"), runtime.handlers) == "Blocked by hook"
    after.assert_not_called()
    s13.release_teammate_assignment("alice")
    assert "Claim a Task" in runtime.write("a.txt", "hello")


def test_worker_model_failure_reports_error_and_returns_task_to_board():
    task = s13.create_task("Work")
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("model unavailable")
    runtime = runtime_for(task, client=client)
    runtime.run()
    messages = s13.consume_lead_inbox()
    assert [message["type"] for message in messages] == ["error"]
    assert "model unavailable" in messages[0]["content"]
    restored = s13.load_task(task.id)
    assert restored.status == "pending" and restored.owner is None
    assert not s13.teammate_assignments and not s13.active_teammates


def test_spawn_start_failure_releases_initial_claim(monkeypatch):
    task = s13.create_task("Work")
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("cannot start")))
    with pytest.raises(RuntimeError, match="cannot start"):
        s13.spawn_teammate_thread("alice", "developer", "Work", task.id,
                                  client=Mock(), model="test")
    assert s13.load_task(task.id).status == "pending"
    assert s13.load_task(task.id).owner is None
    assert not s13.active_teammates and not s13.teammate_threads
    assert not s13.teammate_assignments


def test_failed_claim_save_keeps_disk_and_team_state_consistent(monkeypatch):
    task = s13.create_task("Work")
    saved = s13._task_path(task.id).read_bytes()
    client = Mock()
    monkeypatch.setattr(s13.os, "replace", Mock(side_effect=OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        s13.spawn_teammate_thread("alice", "developer", "Work", task.id,
                                  client=client, model="test")
    assert s13._task_path(task.id).read_bytes() == saved
    assert not list(s13.TASKS_DIR.glob("*.tmp"))
    assert not s13.active_teammates and not s13.teammate_threads
    assert not s13.teammate_assignments
    client.chat.completions.create.assert_not_called()


def test_task_claim_is_also_serialized_between_processes():
    task = s13.create_task("Shared work")
    root = str(Path(s13.__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    code = ("import sys; from experiments import s13_agent_teams as s; "
            "print(s.claim_task(sys.argv[1], sys.argv[2]))")
    processes = [subprocess.Popen([sys.executable, "-c", code, task.id, name],
                                  cwd=s13.WORKDIR, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for name in ("alice", "bob")]
    try:
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            results.append(stdout.splitlines()[-1])
        assert sum(result.startswith("Claimed ") for result in results) == 1
        assert s13.load_task(task.id).owner in {"alice", "bob"}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


@pytest.mark.parametrize("name", ["../outside", "lead", "Agent"])
def test_invalid_teammate_identity_never_starts_thread(name, monkeypatch):
    start = Mock()
    monkeypatch.setattr(threading.Thread, "start", start)
    assert "Invalid teammate name" in s13.spawn_teammate_thread(
        name, "developer", "Work", client=Mock(), model="test",
    )
    start.assert_not_called()
    assert not s13.active_teammates


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd or s13.WORKDIR,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_worktree_routes_tools_and_preserves_changes_and_branch():
    git("init", "-q")
    (s13.WORKDIR / "base.txt").write_text("base")
    git("add", "base.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    task = s13.create_task("Write in worktree")
    assert "created at" in s13.create_worktree("isolated", task.id)
    runtime = runtime_for(task)
    worktree = s13.WORKTREES_DIR / "isolated"
    assert runtime.write("result.txt", "hello").startswith("Wrote ")
    assert runtime.read("result.txt") == "hello"
    assert runtime.bash("git branch --show-current") == "wt/isolated"
    assert not (s13.WORKDIR / "result.txt").exists()
    assert "active task" in s13.remove_worktree("isolated")
    assert runtime.complete(task.id).startswith("Completed ")
    assert "still in use" in s13.remove_worktree("isolated")
    assert runtime.write("after-complete.txt", "same turn").startswith("Wrote ")
    s13.release_completed_assignment("alice")
    assert "uncommitted" in s13.remove_worktree("isolated")
    assert (worktree / "result.txt").read_text() == "hello"
    git("add", "result.txt", "after-complete.txt", cwd=worktree)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "work", cwd=worktree)
    assert "branch 'wt/isolated' retained" in s13.remove_worktree("isolated")
    assert git("show", "wt/isolated:result.txt") == "hello"
    assert s13.load_task(task.id).worktree is None
    assert "remove_worktree" not in {t["function"]["name"] for t in s13.TOOLS}


def test_broken_worktree_binding_does_not_fall_back_to_repository():
    task = s13.create_task("Work")
    task.worktree = "missing"
    s13.save_task(task)
    assert "Cannot claim" in s13.claim_task(task.id, "alice")
    assert not s13.scan_unclaimed_tasks() and not s13.teammate_assignments


def test_lead_loop_pairs_openai_tool_results_and_enforces_call_limit(monkeypatch):
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion([tool("create", "create_task", subject="Work")]), completion(),
    ]
    messages = [{"role": "system", "content": "Lead"}, {"role": "user", "content": "Create work"}]
    s13.agent_loop(messages, client, "test")
    assert messages[-2]["role"] == "tool" and messages[-2]["tool_call_id"] == "create"
    assert messages[-2]["content"].startswith("Created task_")
    client.chat.completions.create.side_effect = None
    client.chat.completions.create.return_value = completion([tool("list", "list_tasks")])
    monkeypatch.setattr(s13, "MAX_TURNS", 1)
    with pytest.raises(RuntimeError, match="Model call limit"):
        s13.agent_loop(messages, client, "test")


def test_truncated_tool_calls_are_never_executed():
    task = s13.create_task("Work")
    client = Mock()
    client.chat.completions.create.return_value = completion(
        [tool("write", "write_file", path="a.txt", content="hello")], reason="length",
    )
    runtime = runtime_for(task, client=client)
    with pytest.raises(RuntimeError, match="Model stopped unexpectedly"):
        runtime.work()
    with pytest.raises(RuntimeError, match="Model stopped unexpectedly"):
        s13.agent_loop([], client, "test")
    assert not (s13.WORKDIR / "a.txt").exists()


def test_import_and_cli_quit_need_no_model(tmp_path):
    env = dict(os.environ, OPENAI_API_KEY="test-key", MODEL_ID="test-model",
               OPENAI_BASE_URL="https://example.invalid/v1")
    script = Path(s13.__file__).resolve()
    for args in (["-c", "import threading; from experiments import s13_agent_teams; "
                  "assert len(threading.enumerate()) == 1"], [str(script)]):
        result = subprocess.run([sys.executable, *args], input="q\n", text=True,
                                capture_output=True, env=env, timeout=15,
                                cwd=script.parents[1] if args[0] == "-c" else tmp_path)
        assert result.returncode == 0, result.stderr
        assert not result.stderr
