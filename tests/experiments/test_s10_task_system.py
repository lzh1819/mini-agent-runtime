"""验证任务图、认领与持久状态，以及工具反馈，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s10_task_system as s10


def configure_store(tmp_path, monkeypatch):
    monkeypatch.setattr(s10, "WORKDIR", tmp_path)
    monkeypatch.setattr(s10, "TASKS", s10.TaskStore(tmp_path / ".tasks"))
    return s10.TASKS


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


def test_dependency_graph_lifecycle_and_reopening_store(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    schema = s10.create_task("Schema", "Define the database tables.")
    api = s10.create_task("API", "Implement endpoints after schema is ready.")
    docs = s10.create_task("Docs")
    tests = s10.create_task("Tests")
    s10.update_task(api.id, [schema.id])
    s10.update_task(docs.id, [schema.id])
    s10.update_task(tests.id, [api.id, docs.id])

    assert not s10.can_start(api.id)
    assert "Blocked by:" in s10.claim_task(api.id)
    assert "cannot complete" in s10.complete_task(schema.id)
    assert s10.load_task(api.id).status == "pending"
    assert s10.claim_task(schema.id).startswith("Claimed")
    first_completion = s10.complete_task(schema.id)
    assert "Unblocked:" in first_completion and "API" in first_completion and "Docs" in first_completion
    assert "Tests" not in first_completion
    assert s10.can_start(api.id) and s10.can_start(docs.id)
    s10.claim_task(api.id, owner="worker")
    assert "owned by worker" in s10.complete_task(api.id)
    assert s10.load_task(api.id).status == "in_progress"
    assert "Unblocked:" not in s10.complete_task(api.id, owner="worker")
    assert not s10.can_start(tests.id)
    s10.claim_task(docs.id)
    assert "Unblocked: Tests" in s10.complete_task(docs.id)
    s10.claim_task(tests.id)
    s10.complete_task(tests.id)
    assert "cannot claim" in s10.claim_task(tests.id)

    reopened = s10.TaskStore(store.directory)
    assert {task.status for task in reopened.list()} == {"completed"}
    assert reopened.load(api.id).owner == "worker"
    assert reopened.load(tests.id).blockedBy == [api.id, docs.id]
    assert json.loads(s10.get_task(schema.id))["description"] == "Define the database tables."


def test_dependency_updates_validate_whole_batch_and_reject_cycles(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    first = store.create("First")
    second = store.create("Second")
    third = store.create("Third")
    missing = store.create("Deleted dependency")
    store._path(missing.id).unlink()
    original = store._path(first.id).read_text()

    with pytest.raises(ValueError, match="Dependency not found"):
        store.update_dependencies(first.id, [second.id, missing.id])
    assert store._path(first.id).read_text() == original
    with pytest.raises(ValueError, match="depend on itself"):
        store.update_dependencies(first.id, [first.id])
    store.update_dependencies(second.id, [first.id, first.id])
    store.update_dependencies(second.id, [first.id])
    assert store.load(second.id).blockedBy == [first.id]
    store.update_dependencies(third.id, [second.id])
    with pytest.raises(ValueError, match="cycle"):
        store.update_dependencies(first.id, [third.id])
    assert store._path(first.id).read_text() == original


@pytest.mark.parametrize("state", ["in_progress", "completed", "pending_owned"])
def test_dependencies_can_only_change_while_pending_and_unowned(tmp_path, monkeypatch, state):
    store = configure_store(tmp_path, monkeypatch)
    task = store.create("Work")
    dependency = store.create("Prerequisite")
    if state == "pending_owned":
        task.owner = "worker"
        store.save(task)
    else:
        s10.claim_task(task.id)
        if state == "completed":
            s10.complete_task(task.id)
    original = store._path(task.id).read_text()

    with pytest.raises(ValueError, match="pending and unowned"):
        store.update_dependencies(task.id, [dependency.id])
    assert store._path(task.id).read_text() == original


def test_missing_dependency_blocks_claim_and_corrupt_dependency_is_visible(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    prerequisite = store.create("Prerequisite")
    task = store.create("Dependent")
    store.update_dependencies(task.id, [prerequisite.id])
    store._path(prerequisite.id).unlink()

    assert not s10.can_start(task.id)
    assert prerequisite.id in s10.claim_task(task.id)
    assert store.load(task.id).status == "pending"
    prerequisite.status = "invalid"
    store.save(prerequisite)
    with pytest.raises(ValueError, match="Invalid task status"):
        s10.claim_task(task.id)
    assert store.load(task.id).status == "pending"


def test_random_id_collision_does_not_overwrite_existing_task(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(s10.secrets, "token_hex", Mock(side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"]))
    first = store.create("First", "Keep this record.")
    original = store._path(first.id).read_text()

    second = store.create("Second")

    assert second.id == "task_bbbbbbbb"
    assert store._path(first.id).read_text() == original
    assert len(store.list()) == 2


def test_model_uses_returned_ids_and_receives_task_and_file_results(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    requests = []
    ids = {}
    executed = []
    monkeypatch.setitem(s10.HOOKS, "PostToolUse", [
        lambda name, args, output: executed.append(name),
    ])

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        turn = len(requests)
        if turn == 1:
            return completion([tool("prepare", "create_task", subject="Prepare"),
                               tool("build", "create_task", subject="Build")])
        if turn == 2:
            for message in kwargs["messages"][-2:]:
                ids[message["tool_call_id"]] = message["content"].split()[1].removesuffix(":")
            return completion([
                tool("depend", "update_task", task_id=ids["build"], addBlockedBy=[ids["prepare"]]),
                tool("blocked", "claim_task", task_id=ids["build"]),
            ])
        if turn == 3:
            return completion([
                tool("claim", "claim_task", task_id=ids["prepare"]),
                tool("write", "write_file", path="result.txt", content="hello"),
                tool("complete", "complete_task", task_id=ids["prepare"]),
            ])
        if turn == 4:
            return completion([tool("claim-build", "claim_task", task_id=ids["build"]),
                               tool("complete-build", "complete_task", task_id=ids["build"])])
        if turn == 5:
            return completion([tool("detail", "get_task", task_id=ids["build"]),
                               tool("list", "list_tasks")])
        return completion()

    client = Mock()
    client.chat.completions.create.side_effect = create
    messages = [{"role": "system", "content": s10.SYSTEM}, {"role": "user", "content": "Create a task graph."}]
    s10.agent_loop(messages, client, "test")

    assert requests[2]["messages"][-1]["content"].startswith("Blocked by:")
    assert "Unblocked: Build" in requests[3]["messages"][-1]["content"]
    assert json.loads(requests[5]["messages"][-2]["content"])["status"] == "completed"
    assert requests[5]["messages"][-1]["content"].count("[x]") == 2
    assert {task.status for task in store.list()} == {"completed"}
    assert (tmp_path / "result.txt").read_text() == "hello"
    assert {"create_task", "update_task", "claim_task", "complete_task", "get_task", "list_tasks"} <= set(executed)
    assert {item["function"]["name"] for item in requests[0]["tools"]} == set(s10.TOOL_HANDLERS)


def test_pre_hook_can_block_task_creation(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    monkeypatch.setitem(s10.HOOKS, "PreToolUse", [lambda name, args: "Task creation denied."])
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion([tool("create", "create_task", subject="Do not create")]), completion(),
    ]
    messages = [{"role": "user", "content": "Create a task."}]

    s10.agent_loop(messages, client, "test")

    assert messages[-2]["content"] == "Task creation denied."
    assert not store.directory.exists()


def test_task_ids_and_symlinks_cannot_escape_store(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    task = store.create("Work")
    original = store._path(task.id).read_text()
    external = tmp_path / "external.json"
    external.write_text(original)
    store._path(task.id).unlink()
    (store.directory / f"{task.id}.json").symlink_to(external)

    with pytest.raises(ValueError):
        store.load("../external")
    with pytest.raises(ValueError, match="symlink"):
        store.load(task.id)
    with pytest.raises(ValueError, match="symlink"):
        store.save(task)
    assert external.read_text() == original


def test_task_store_cannot_create_directories_outside_workspace(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(s10, "WORKDIR", workdir)
    external = tmp_path / "outside"
    external.mkdir()
    (workdir / "linked").symlink_to(external, target_is_directory=True)
    store = s10.TaskStore(workdir / "linked" / ".tasks")

    with pytest.raises(ValueError, match="escapes the workspace"):
        store.create("Do not create outside")
    assert list(external.iterdir()) == []


def test_task_file_id_must_match_requested_id(tmp_path, monkeypatch):
    store = configure_store(tmp_path, monkeypatch)
    first = store.create("First")
    second = store.create("Second")
    store._path(first.id).write_text(store._path(second.id).read_text())

    with pytest.raises(ValueError, match="does not match"):
        store.load(first.id)
