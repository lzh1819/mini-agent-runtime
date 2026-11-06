"""验证工作流编排、日志恢复和 S15 的 OpenAI 工具调用，不请求真实模型。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion

from experiments import s16_workflow_runtime as s16


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(s16, "STORE", tmp_path / ".runtime")
    monkeypatch.setattr(s16, "RUNNER_FACTORY", s16.MockAgentRunner)


def completion(text, calls=None, tokens=7):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": text,
                                 "tool_calls": calls}}],
        "usage": {"prompt_tokens": tokens, "completion_tokens": 2,
                  "total_tokens": tokens + 2},
    })


def test_demo_and_resume_work_across_processes_without_credentials(tmp_path):
    script = tmp_path / "s16_workflow_runtime.py"
    shutil.copy2(s16.__file__, script)
    env = {key: value for key, value in os.environ.items()
           if key not in {"OPENAI_API_KEY", "MODEL_ID"}}

    def run(mode):
        return subprocess.run(
            [sys.executable, str(script), mode], cwd=tmp_path, env=env,
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout

    first = run("demo")
    resumed = run("resume")
    assert "async_launched" in first
    assert "status=completed" in first
    assert "status=cached" in resumed
    assert "status=completed  agents=0  tokens=0" in resumed
    run_id = (tmp_path / ".runtime" / "last_run.txt").read_text()
    snapshot = json.loads((tmp_path / ".runtime" / f"{run_id}.json").read_text())
    assert snapshot["args"]["changes"] == s16.DEMO_CHANGES


def test_workflow_runs_through_real_host_and_openai_sdk(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    host = s16.load_integrated_host()
    host.MODEL = host.PRIMARY_MODEL = "test"
    host.MEMORY_RUNTIME.request_json_array = Mock(return_value=[])
    requests = []
    changes = "query = f'SELECT * FROM users WHERE id = {user_id}'"

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            names = [tool["function"]["name"] for tool in payload["tools"]]
            assert len(names) == 27
            assert "read_file" in names
            assert names[-1] == "Workflow"
            if payload["messages"][-1]["role"] == "tool":
                result = json.loads(payload["messages"][-1]["content"])
                assert result["task"]["status"] == "completed"
                assert result["task"]["usage"] == {"agents": 8, "tokens": 72}
                assert len(result["result"]["confirmed"]) == 4
                assert payload["messages"][-1]["tool_call_id"] == "review"
                reply = completion("Four dimensions reviewed")
            else:
                reply = completion(None, [{
                    "id": "review", "type": "function", "function": {
                        "name": "Workflow", "arguments": json.dumps({
                            "name": "review-changes", "args": {"changes": changes},
                        }),
                    },
                }])
        else:
            assert payload["max_completion_tokens"] == 2000
            assert len(payload["messages"]) == 2
            assert "no direct shell or file tools" in payload["messages"][0]["content"]
            prompt = payload["messages"][1]["content"]
            assert changes in prompt
            if "Adversarially verify" in prompt:
                reply = completion('{"isReal": true, "reason": "supported by input"}')
            else:
                reply = completion('{"findings": [{"title": "unsafe SQL", "severity": "high"}]}')
        return httpx2.Response(200, json=reply.model_dump(exclude_none=True))

    with OpenAI(api_key="test", max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
        host.client = client
        s16.install_workflow_tool(host)
        s16.install_workflow_tool(host)
        assert isinstance(s16.RUNNER_FACTORY(), s16.OpenAIAgentRunner)
        messages = [{"role": "user", "content": "Review the supplied changes using Workflow"}]
        host.agent_loop(messages, {}, messages[0]["content"])

    assert len(requests) == 10
    assert host.extract_text(messages[-1]["content"]) == "Four dimensions reviewed"
    run_id = (s16.STORE / "last_run.txt").read_text()
    snapshot = json.loads((s16.STORE / f"{run_id}.json").read_text())
    assert snapshot["args"] == {"changes": changes}
    assert snapshot["task"]["status"] == "completed"
    assert host.assemble_tool_pool()[1]["Workflow"](name="missing") == (
        "Error: unknown workflow 'missing'"
    )


@pytest.mark.parametrize("valid_retry", [True, False])
def test_schema_retry_records_usage_or_failure(monkeypatch, valid_retry):
    replies = [completion("invalid JSON", tokens=3), completion(
        'Result:\n```json\n{"ok": true}\n```' if valid_retry else '{"ok": "yes"}',
        tokens=4,
    )]
    client = Mock()
    client.chat.completions.create.side_effect = replies
    monkeypatch.setattr(s16, "RUNNER_FACTORY", lambda: s16.OpenAIAgentRunner(client, "test"))

    async def workflow(ctx, _args):
        return await ctx.agent("Check input", schema={
            "type": "object", "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        })

    out = asyncio.run(s16.WorkflowTool().call(
        {"name": "schema-check", "description": "Check output schema"}, workflow,
    ))
    assert client.chat.completions.create.call_count == 2
    assert "Return valid JSON." in client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
    if valid_retry:
        assert out["task"].status == "completed"
        assert out["result"] == {"ok": True}
        assert out["task"].usage == {"agents": 1, "tokens": 11}
    else:
        assert out["task"].status == "failed"
        assert "invalid output" in out["result"]["error"]
        assert not (s16.STORE / f"{out['task'].run_id}.journal.jsonl").read_text()


def test_resume_after_failure_reuses_only_completed_agent_calls(monkeypatch):
    calls = []

    class Runner:
        fail = True

        def run(self, prompt, schema=None, label=None):
            calls.append(prompt)
            if prompt == "second" and self.fail:
                raise RuntimeError("interrupted")
            return s16.RunnerOutput(prompt.upper(), 1)

    runner = Runner()
    monkeypatch.setattr(s16, "RUNNER_FACTORY", lambda: runner)

    async def workflow(ctx, _args):
        first = await ctx.agent("first")
        second = await ctx.agent("second")
        return [first, second]

    meta = {"name": "recover", "description": "Resume partial work"}
    failed = asyncio.run(s16.WorkflowTool().call(meta, workflow, {"input": "same"}))
    assert failed["task"].status == "failed"
    runner.fail = False
    resumed = asyncio.run(s16.WorkflowTool().call(
        meta, workflow, resume_from_run_id=failed["task"].run_id,
    ))
    assert resumed["result"] == ["FIRST", "SECOND"]
    assert resumed["task"].usage == {"agents": 1, "tokens": 1}
    assert calls == ["first", "second", "second"]
    assert resumed["task"].progress[0]["status"] == "cached"


def test_pipeline_advances_items_without_waiting_for_other_items(monkeypatch):
    verified = threading.Event()

    class Runner:
        def run(self, prompt, schema=None, label=None):
            if prompt == "slow audit":
                assert verified.wait(2), "pipeline waited for every audit before verification"
            if prompt == "fast verify":
                verified.set()
            return s16.RunnerOutput(prompt, 1)

    monkeypatch.setattr(s16, "RUNNER_FACTORY", Runner)

    async def workflow(ctx, _args):
        async def audit(_value, item, _index):
            return await ctx.agent(f"{item} audit")

        async def verify(value, item, _index):
            assert value == f"{item} audit"
            return await ctx.agent(f"{item} verify")

        return await ctx.pipeline(["slow", "fast"], audit, verify)

    out = asyncio.run(s16.WorkflowTool().call(
        {"name": "pipeline", "description": "Each item advances independently"}, workflow,
    ))
    assert out["task"].status == "completed"
    assert out["result"] == ["slow verify", "fast verify"]
    assert out["task"].usage == {"agents": 4, "tokens": 4}


def test_parallel_calls_execute_concurrently(monkeypatch):
    barrier = threading.Barrier(2)

    class Runner:
        def run(self, prompt, schema=None, label=None):
            barrier.wait(timeout=2)
            return s16.RunnerOutput(prompt, 1)

    monkeypatch.setattr(s16, "RUNNER_FACTORY", Runner)

    async def workflow(ctx, _args):
        return await ctx.parallel([
            lambda: ctx.agent("first"), lambda: ctx.agent("second"),
        ])

    out = asyncio.run(s16.WorkflowTool().call(
        {"name": "parallel", "description": "Wait for both results"}, workflow,
    ))
    assert out["task"].status == "completed"
    assert out["result"] == ["first", "second"]


def test_resume_validation_preserves_existing_artifacts():
    first = asyncio.run(s16.run_workflow("review-changes", {"changes": s16.DEMO_CHANGES}))
    run_id = first["task"]["runId"]
    paths = [s16.STORE / f"{run_id}.json", s16.STORE / f"{run_id}.output.json"]
    saved = [path.read_text() for path in paths]
    with pytest.raises(s16.WorkflowInputError, match="args do not match"):
        asyncio.run(s16.run_workflow("review-changes", {"changes": "different"}, run_id))
    assert [path.read_text() for path in paths] == saved
    (s16.STORE / f"{run_id}.journal.jsonl").write_text("broken\n")
    with pytest.raises(s16.WorkflowInputError, match="invalid resume journal"):
        asyncio.run(s16.run_workflow("review-changes", resume_from_run_id=run_id))
    assert [path.read_text() for path in paths] == saved
    assert set(s16.WORKFLOW_TOOL["input_schema"]["properties"]) == {
        "name", "args", "resume_from_run_id",
    }
    with pytest.raises(s16.WorkflowInputError, match="invalid workflow runId"):
        asyncio.run(s16.run_workflow("review-changes", resume_from_run_id="../escape"))


def test_active_run_cannot_be_resumed_concurrently():
    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def workflow(_ctx, _args):
            started.set()
            await release.wait()
            return "done"

        meta = {"name": "locked", "description": "Keep one active run"}
        first = asyncio.create_task(s16.WorkflowTool().call(meta, workflow))
        await started.wait()
        run_id = next(s16.STORE.glob("*.journal.jsonl")).name.removesuffix(".journal.jsonl")
        try:
            with pytest.raises(s16.WorkflowInputError, match="already active"):
                await s16.WorkflowTool().call(meta, workflow, resume_from_run_id=run_id)
        finally:
            release.set()
        return await first

    assert asyncio.run(exercise())["result"] == "done"


def test_nested_workflows_share_the_agent_cap_and_budget(monkeypatch):
    async def child(ctx, _args):
        return await ctx.agent("child")

    meta = {"name": "parent", "description": "Share limits with child"}
    monkeypatch.setitem(s16.WORKFLOWS, "child", ({"name": "child"}, child))
    monkeypatch.setattr(s16, "AGENT_CAP", 1)

    async def parent(ctx, _args):
        await ctx.agent("parent")
        return await ctx.workflow("child")

    capped = asyncio.run(s16.WorkflowTool().call(meta, parent))
    assert capped["task"].status == "failed"
    assert "agent() cap reached" in capped["result"]["error"]
    monkeypatch.setattr(s16, "AGENT_CAP", 1000)
    budgeted = asyncio.run(s16.WorkflowTool().call(meta, parent, {"budget": 0}))
    assert budgeted["task"].status == "failed"
    assert budgeted["result"] == {"error": "token budget exceeded"}


def test_cli_initializes_openai_host_before_starting_services(monkeypatch):
    host = s16.load_integrated_host()
    monkeypatch.setattr(s16, "load_integrated_host", lambda: host)
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.delenv("FALLBACK_MODEL_ID", raising=False)
    monkeypatch.setattr(host, "load_dotenv", Mock())
    client = Mock()
    monkeypatch.setattr(host, "OpenAI", Mock(return_value=client))
    monkeypatch.setattr(host.atexit, "register", Mock())
    monkeypatch.setattr(host.signal, "signal", Mock())
    monkeypatch.setattr(host, "start_runtime_services", Mock())
    monkeypatch.setattr(host.CONSOLE, "ask", Mock(return_value="q"))
    monkeypatch.setattr(s16.threading, "Thread", Mock())
    s16.run_cli()
    host.OpenAI.assert_called_once_with(max_retries=0)
    host.load_dotenv.assert_called_once_with(Path(s16.__file__).resolve().parents[1] / ".env")
    assert host.MODEL == host.PRIMARY_MODEL == "test-model"
    assert host.FALLBACK_MODEL is None
    assert host.client is client
    assert s16.RUNNER_FACTORY().client is client
    assert "Workflow" in host.assemble_tool_pool()[1]
    client.chat.completions.create.assert_not_called()
