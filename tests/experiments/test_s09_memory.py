"""验证跨会话记忆、选择性召回与整理的数据保护，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s09_memory as s09


def configure_store(tmp_path, monkeypatch):
    monkeypatch.setattr(s09, "WORKDIR", tmp_path)
    monkeypatch.setattr(s09, "MEMORY_DIR", tmp_path / ".memory")
    monkeypatch.setattr(s09, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")


def record(name="indentation", **changes):
    return {"name": name, "type": "user", "description": "Indentation preference",
            "body": "Use tabs for indentation.", "scope": "persistent", **changes}


def write_record(item):
    return s09.write_memory_file(item["name"], item["type"], item["description"], item["body"])


def completion(content="done", calls=None, finish_reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0,
                     "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else content}}],
    })


def array_response(items):
    return completion(json.dumps(items, ensure_ascii=False))


def fake_client(responses):
    responses = iter(responses)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    client = Mock()
    client.chat.completions.create.side_effect = create
    return client, requests


def snapshot():
    return {path.name: path.read_text() for path in s09.MEMORY_DIR.glob("*.md")}


def test_completed_task_persists_memory_and_fresh_conversation_recalls_it(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    client, requests = fake_client([
        completion(calls=[{"id": "write", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "hello"}),
        }}]),
        completion("Created a.txt. I will remember your preference."),
        array_response([record()]),
    ])
    messages = [{"role": "user", "content": "Create a.txt. Remember that I prefer tabs."}]

    s09.agent_loop(messages, client, "test")

    assert "tools" in requests[0]
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert "[indentation](indentation.md)" in s09.read_memory_index()
    assert "Use tabs for indentation." in s09.read_memory_file("indentation.md")
    extraction_prompt = requests[-1]["messages"][1]["content"]
    assert "Created a.txt." in extraction_prompt
    assert "Wrote 5 bytes to a.txt" not in extraction_prompt
    write_record(record("z-deployment", type="project", description="Deployment information",
                        body="Unrelated deployment details must stay out of recall."))

    # A new message list has no access to the preceding conversation.
    fresh = [{"role": "user", "content": "Which indentation should I use?"}]
    client, requests = fake_client([
        array_response([0]), completion("Use tabs."), array_response([]),
    ])
    s09.agent_loop(fresh, client, "test")

    assert "Use tabs for indentation." not in requests[0]["messages"][1]["content"]
    system = requests[1]["messages"][0]["content"]
    assert "Use tabs for indentation." in system
    assert "Unrelated deployment details must stay out of recall." not in system
    assert "current user request takes priority" in system
    assert requests[1]["messages"][1] == {"role": "user", "content": "Which indentation should I use?"}
    assert len([message for message in fresh if message["role"] == "system"]) == 1


def test_extraction_rejects_temporary_and_duplicate_candidates(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    write_record(record())
    client, _ = fake_client([array_response([
        record("temporary", scope="current_task", description="One task", body="Do not create files."),
        record("session-rule", description="A restriction", body="Do not create files in this session."),
        record("INDENTATION", description="Same name", body="Tabs are preferred."),
        record("same-description", body="A different sentence."),
        record("same-body", description="Same content", body=" USE  TABS for indentation. "),
        record("build-tool", type="project", description="Build command", body="Use uv for this project."),
    ])])

    assert s09.extract_memories([{"role": "user", "content": "Use uv for this project."}], client, "test") == 1
    assert {item["filename"] for item in s09.list_memory_files()} == {"indentation.md", "build-tool.md"}


def test_recall_deduplicates_selection_and_limits_loaded_content(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    write_record(record("alpha", body="a" * 3000))
    write_record(record("beta", description="Another preference", body="b" * 3000))
    monkeypatch.setattr(s09, "RECALL_CHAR_LIMIT", 1000)
    client, _ = fake_client([array_response([1, 1, 0])])

    loaded = json.loads(s09.load_memories([{"role": "user", "content": "Preferences?"}], client, "test"))

    assert [item["source"] for item in loaded] == ["beta.md"]
    assert sum(len(item["content"]) for item in loaded) == 1000


def test_bounded_inputs_keep_newest_dialogue_and_exclude_system_and_tools():
    messages = [
        {"role": "system", "content": "Recalled background must not be extracted again."},
        {"role": "user", "content": "Earlier discussion " * 1000},
        {"role": "tool", "content": "A tool result is not user feedback."},
        {"role": "user", "content": "Remember to use uv for this project."},
    ]

    query = s09.recent_user_text(messages)
    dialogue = s09.dialogue_text(messages)

    assert len(query) <= 4000 and len(dialogue) <= 8000
    assert query.endswith("Remember to use uv for this project.")
    assert dialogue.endswith("Remember to use uv for this project.")
    assert "Recalled background" not in dialogue
    assert "A tool result" not in dialogue


def test_stop_continuation_finishes_before_extracting_memory(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    stop = Mock(side_effect=["Check the conclusion again.", None])
    monkeypatch.setitem(s09.HOOKS, "Stop", [stop])
    client, requests = fake_client([
        completion("Tentative conclusion."), completion("Verified conclusion."), array_response([]),
    ])

    s09.agent_loop([{"role": "user", "content": "Inspect the project."}], client, "test")

    assert [request["max_completion_tokens"] for request in requests] == [8000, 8000, 1000]
    assert "Verified conclusion." in requests[-1]["messages"][1]["content"]
    assert stop.call_count == 2
    assert not s09.MEMORY_DIR.exists()


def test_consolidation_replaces_records_and_rebuilds_index(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(s09, "CONSOLIDATE_THRESHOLD", 2)
    write_record(record("old-formatting"))
    write_record(record("another-formatting", description="Formatting details", body="Tabs, please."))
    client, _ = fake_client([array_response([record("formatting", body="Use tabs in Python files.")])])

    assert s09.consolidate_memories(client, "test") == 1
    assert set(snapshot()) == {"formatting.md", "MEMORY.md"}
    assert "Use tabs in Python files." in s09.read_memory_file("formatting.md")
    assert "[formatting](formatting.md)" in s09.read_memory_index()


@pytest.mark.parametrize("failure_stage", ["write", "index"])
def test_failed_consolidation_restores_original_files(tmp_path, monkeypatch, failure_stage):
    configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(s09, "CONSOLIDATE_THRESHOLD", 2)
    write_record(record("first"))
    write_record(record("second", description="Another preference", body="Keep functions small."))
    original = snapshot()
    failure = OSError("simulated storage failure")
    if failure_stage == "index":
        original_rebuild = s09.rebuild_memory_index
        rebuild = Mock(side_effect=[failure, None])

        def rebuild_index():
            rebuild()
            original_rebuild()

        monkeypatch.setattr(s09, "rebuild_memory_index", rebuild_index)
    else:
        original_write = type(tmp_path).write_text
        failed = False

        def write_text(path, *args, **kwargs):
            nonlocal failed
            if path.name == "merged.md" and not failed:
                failed = True
                raise failure
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(type(tmp_path), "write_text", write_text)
    client, _ = fake_client([array_response([
        record("new-record"), record("merged", description="Combined preference", body="Use short functions."),
    ])])

    with pytest.raises(OSError) as caught:
        s09.consolidate_memories(client, "test")
    assert caught.value is failure
    assert snapshot() == original


@pytest.mark.parametrize("replacement", [
    [], [record("same"), record("SAME")], [record("valid"), {"name": "invalid"}],
])
def test_invalid_consolidation_never_replaces_existing_data(tmp_path, monkeypatch, replacement):
    configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(s09, "CONSOLIDATE_THRESHOLD", 1)
    write_record(record())
    original = snapshot()
    client, _ = fake_client([array_response(replacement)])

    with pytest.raises(ValueError):
        s09.consolidate_memories(client, "test")
    assert snapshot() == original


@pytest.mark.parametrize("response,error_type", [
    (completion("not JSON"), ValueError),
    (completion("{}"), ValueError),
    (array_response([record(), {"name": "missing fields"}]), ValueError),
    (completion("[]", finish_reason="length"), RuntimeError),
])
def test_invalid_extraction_is_visible_and_preserves_store(tmp_path, monkeypatch, response, error_type):
    configure_store(tmp_path, monkeypatch)
    write_record(record())
    original = snapshot()
    client, _ = fake_client([response])

    with pytest.raises(error_type):
        s09.extract_memories([{"role": "user", "content": "Remember my preferences."}], client, "test")
    assert snapshot() == original


def test_memory_paths_cannot_escape_or_replace_index(tmp_path, monkeypatch):
    configure_store(tmp_path, monkeypatch)
    s09.MEMORY_DIR.mkdir()
    external = tmp_path / "external.md"
    external.write_text("Unchanged external data")
    (s09.MEMORY_DIR / "link.md").symlink_to(external)

    for filename in ("../external.md", "MEMORY.md", "memory.md", "link.md"):
        with pytest.raises(ValueError):
            s09.read_memory_file(filename)
    with pytest.raises(ValueError):
        s09.write_memory_file("link", "project", "Attempted overwrite", "New content")
    assert external.read_text() == "Unchanged external data"


def test_memory_directory_symlink_cannot_write_outside_workdir(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    configure_store(workdir, monkeypatch)
    external = tmp_path / "outside"
    external.mkdir()
    s09.MEMORY_DIR.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes the workspace"):
        write_record(record())
    assert list(external.iterdir()) == []
