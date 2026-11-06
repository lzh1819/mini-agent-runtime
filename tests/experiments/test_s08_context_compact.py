"""验证压缩保留的数据、工具配对和超限重试，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import httpx2
import pytest
from openai import BadRequestError
from openai.types.chat import ChatCompletion

from experiments import s08_context_compact as s08


def tool(call_id, name="read_file", **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def completion(calls=None, content="done", finish_reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0,
                     "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else content}}],
    })


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


def compactor(tmp_path, client=None):
    return s08.ContextCompactor(client or Mock(), "test", tmp_path / "transcripts", tmp_path / "outputs")


def history():
    return [{"role": "system", "content": s08.SYSTEM},
            {"role": "user", "content": "Inspect the project, then run tests."}]


def batch(prefix, outputs):
    calls = [tool(f"{prefix}-{index}", path=f"{index}.txt") for index in range(len(outputs))]
    return [completion(calls).choices[0].message.model_dump(exclude_none=True), *[
        {"role": "tool", "tool_call_id": call["id"], "content": output}
        for call, output in zip(calls, outputs)
    ]]


def assert_paired(messages):
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


def configure_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(s08, "WORKDIR", tmp_path)
    monkeypatch.setattr(s08, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(s08, "TOOL_RESULTS_DIR", tmp_path / "outputs")


def api_error(code):
    return BadRequestError(code, body={"code": code}, response=httpx2.Response(
        400, request=httpx2.Request("POST", "https://example.invalid/v1/chat/completions"),
    ))


def test_latest_batch_budget_preserves_full_outputs(tmp_path):
    compact = compactor(tmp_path)
    compact.LARGE_RESULT_CHAR_LIMIT = 1000
    messages = history() + batch("old", ["o" * 7000]) + batch("new", ["a" * 8000, "b" * 2000])

    compact.tool_result_budget(messages, max_chars=5000)

    assert messages[3]["content"] == "o" * 7000
    saved = compact.persisted_output_path(messages[-2]["content"])
    assert saved is not None
    assert messages[-1]["content"] == "b" * 2000
    assert sum(len(message["content"]) for message in messages[-2:]) <= 5000
    assert next(compact.tool_results_dir.glob("*.txt")).read_text() == "a" * 8000
    assert_paired(messages)
    compact.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("max_messages", [8, 9, 10])
def test_archive_cuts_preserve_whole_batches(tmp_path, max_messages):
    compact = compactor(tmp_path)
    messages = history()
    for index in range(5):
        messages.extend(batch(str(index), ["first", "second", "third"]))
    original = deepcopy(messages)

    shortened = compact.snip_compact(messages, max_messages)

    assert_paired(shortened)
    assert shortened[:2] == original[:2]
    assert shortened[-4:] == original[-4:]
    assert any(compact.is_archive_marker(message) for message in shortened)
    transcript = next(compact.transcript_dir.glob("*.jsonl"))
    assert [json.loads(line) for line in transcript.read_text().splitlines()] == original


def test_micro_compact_keeps_recent_and_unseen_results(tmp_path):
    compact = compactor(tmp_path)
    messages = history()
    for index in range(5):
        messages.extend(batch(f"old-{index}", [str(index) * 3000]))
    messages.extend(batch("unseen", ["new" * 2000, "latest" * 1000]))

    compact.micro_compact(messages)

    results = [message for message in messages if message["role"] == "tool"]
    assert all(message["content"].startswith("[Earlier tool result saved at ") for message in results[:2])
    assert [message["content"] for message in results[2:]] == [
        "2" * 3000, "3" * 3000, "4" * 3000, "new" * 2000, "latest" * 1000,
    ]
    assert sorted(path.read_text() for path in compact.tool_results_dir.glob("*.txt")) == [
        "0" * 3000, "1" * 3000,
    ]
    compact.client.chat.completions.create.assert_not_called()


def test_prepare_previews_large_unseen_results_before_summarizing(tmp_path):
    compact = compactor(tmp_path)
    compact.CONTEXT_CHAR_LIMIT = 10000
    messages = history() + batch("new", ["x" * 40000])

    prepared = compact.prepare(messages, "Inspect the project.")

    assert compact.estimate_chars(prepared) < compact.CONTEXT_CHAR_LIMIT
    assert prepared[-1]["content"].startswith("<persisted-output>")
    assert next(compact.tool_results_dir.glob("*.txt")).read_text() == "x" * 40000
    compact.client.chat.completions.create.assert_not_called()


def test_auto_summary_preserves_system_current_request_and_transcript(tmp_path):
    client, requests = fake_client([completion(content="Read a.py. Testing remains.")])
    compact = compactor(tmp_path, client)
    messages = history() + [{"role": "assistant", "content": "old detail " * 9000}]
    original = deepcopy(messages)

    prepared = compact.prepare(messages, "Run the tests now.")

    assert prepared[0] == original[0]
    assert len(prepared) == 2
    assert "Current user request:\nRun the tests now." in prepared[1]["content"]
    assert "Conversation summary (reference only):" in prepared[1]["content"]
    assert "Read a.py. Testing remains." in prepared[1]["content"]
    transcript = next(compact.transcript_dir.glob("*.jsonl"))
    assert [json.loads(line) for line in transcript.read_text().splitlines()] == original
    assert len(requests[0]["messages"][1]["content"]) < 81000
    assert "tools" not in requests[0]
    assert messages == original


def test_manual_compact_waits_for_every_tool_result(tmp_path, monkeypatch):
    configure_workdir(tmp_path, monkeypatch)
    client, requests = fake_client([
        completion([tool("compact", "compact"),
                    tool("write", "write_file", path="a.txt", content="hello")]),
        completion(content="Created a.txt with hello; verification remains."),
        completion(content="Finished."),
    ])
    messages = history()

    s08.agent_loop(messages, client, "test", "Create a.txt and verify it.")

    transcript = next((tmp_path / "transcripts").glob("*.jsonl"))
    archived = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert_paired(archived)
    assert [message["tool_call_id"] for message in archived if message["role"] == "tool"] == [
        "compact", "write",
    ]
    assert archived[-1]["content"] == "Wrote 5 bytes to a.txt"
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert json.loads(requests[1]["messages"][1]["content"])[-1] == archived[-1]
    assert requests[2]["messages"][0] == history()[0]
    assert "Current user request:\nCreate a.txt and verify it." in requests[2]["messages"][1]["content"]
    assert messages[-1]["content"] == "Finished."


def test_pre_hook_can_block_manual_compaction(tmp_path, monkeypatch):
    configure_workdir(tmp_path, monkeypatch)
    monkeypatch.setitem(s08.HOOKS, "PreToolUse", [lambda name, args: "Blocked compact."])
    client, requests = fake_client([completion([tool("compact", "compact")]), completion()])
    messages = history()

    s08.agent_loop(messages, client, "test", "Inspect the project.")

    assert len(requests) == 2
    assert messages[-2]["content"] == "Blocked compact."
    assert not (tmp_path / "transcripts").exists()


def test_reactive_compact_preserves_the_entire_recent_batch(tmp_path, monkeypatch):
    configure_workdir(tmp_path, monkeypatch)
    client, requests = fake_client([
        api_error("context_length_exceeded"), completion(content="Earlier investigation summary."),
        completion(content="Finished."),
    ])
    messages = history() + [
        {"role": "assistant" if index % 2 else "user", "content": f"Earlier discussion {index}"}
        for index in range(6)
    ] + batch("recent", [f"Result {index}" for index in range(6)])
    recent = deepcopy(messages[-7:])

    s08.agent_loop(messages, client, "test", "Continue testing.")

    retry = requests[2]["messages"]
    assert_paired(retry)
    assert retry[-7:] == recent
    assert retry[0] == history()[0]
    assert "[Reactive compact]" in retry[1]["content"]
    assert "Current user request:\nContinue testing." in retry[1]["content"]


@pytest.mark.parametrize("code,call_count", [
    ("context_length_exceeded", 3), ("invalid_parameter", 1),
])
def test_reactive_retry_is_bounded_and_specific(tmp_path, monkeypatch, code, call_count):
    configure_workdir(tmp_path, monkeypatch)
    error = api_error(code)
    client, requests = fake_client([error, completion(content="Summary."), error])

    with pytest.raises(BadRequestError) as caught:
        s08.agent_loop(history(), client, "test", "Inspect the project.")
    assert caught.value is error
    assert len(requests) == call_count


@pytest.mark.parametrize("response", [
    completion(content=""), completion(content="Partial summary", finish_reason="length"),
])
def test_failed_summary_preserves_history_and_archive(tmp_path, response):
    client, _ = fake_client([response])
    compact = compactor(tmp_path, client)
    messages = history()
    original = deepcopy(messages)

    with pytest.raises(RuntimeError):
        compact.compact_history(messages, "Continue the task.")
    assert messages == original
    transcript = next(compact.transcript_dir.glob("*.jsonl"))
    assert [json.loads(line) for line in transcript.read_text().splitlines()] == original


def test_persisted_outputs_reuse_originals_without_overwriting_or_following_external_paths(tmp_path):
    compact = compactor(tmp_path)
    original = "original data " * 1000
    preview = compact.persisted_preview("../../call", original)
    path = compact.persisted_output_path(preview)

    shorter = compact.persisted_preview("../../call", preview, preview_chars=1000)
    assert compact.persisted_output_path(shorter) == path
    assert len(list(compact.tool_results_dir.glob("*.txt"))) == 1
    second = compact.save_output("../../call", "second output")
    assert str(second) != path
    assert next(path for path in compact.tool_results_dir.glob("*.txt") if path != second).read_text() == original
    external = tmp_path / "external.txt"
    external.write_text("Do not read this via a forged output marker.")
    assert compact.persisted_output_path(f"<persisted-output>\nFull output: {external}\n") is None
