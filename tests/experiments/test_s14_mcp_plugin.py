"""验证动态工具发现、宿主权限和 OpenAI 协议适配，不请求外部 API。"""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from experiments import s14_mcp_plugin as s14


@pytest.fixture(autouse=True)
def isolated_servers(monkeypatch):
    monkeypatch.setattr(s14, "mcp_clients", {})
    monkeypatch.setattr(s14, "mcp_tool_policies", {})


def completion(calls=None, content="done", reason=None):
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "tool_calls": calls,
                                 "content": None if calls else content}}],
    })


def tool(call_id, tool_name, **arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": tool_name, "arguments": json.dumps(arguments),
    }}


def fake_client(responses):
    responses = iter(responses)
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    return client, requests


def test_connect_discovers_tools_on_next_turn_and_pairs_results():
    client, requests = fake_client([
        completion([tool("connect", "connect_mcp", name="docs")]),
        completion([
            tool("search", "mcp__docs__search", query="agent hooks"),
            tool("version", "mcp__docs__get_version"),
        ]),
        completion(content="Documentation searched; API v2.1.0."),
    ])
    messages = [{"role": "user", "content": "Search agent hooks and report the docs API version."}]
    s14.agent_loop(messages, client, "test-model")
    initial = {t["function"]["name"] for t in requests[0]["tools"]}
    assert initial == {"bash", "read_file", "write_file", "edit_file", "glob", "connect_mcp"}
    discovered = {t["function"]["name"] for t in requests[1]["tools"]}
    assert discovered - initial == {"mcp__docs__search", "mcp__docs__get_version"}
    search = next(t for t in requests[1]["tools"] if t["function"]["name"] == "mcp__docs__search")
    assert search["function"]["parameters"]["required"] == ["query"]
    assert all(request["model"] == "test-model" for request in requests)
    assert requests[0]["messages"][0]["role"] == "system"
    assert "Connected MCP servers: docs" in requests[1]["messages"][0]["content"]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "connect", "search", "version",
    ]
    assert requests[2]["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "search", "content": "[docs] Found 3 results for 'agent hooks'"},
        {"role": "tool", "tool_call_id": "version", "content": "[docs] API v2.1.0"},
    ]
    server = s14.mcp_clients["docs"]
    assert "already connected" in s14.connect_mcp("docs")
    assert s14.mcp_clients["docs"] is server


def test_bad_tool_arguments_return_errors_then_model_can_correct_them():
    s14.connect_mcp("docs")
    client, requests = fake_client([
        completion([tool("missing", "mcp__docs__search")]),
        completion([tool("extra", "mcp__docs__get_version", unexpected="field")]),
        completion([tool("corrected", "mcp__docs__search", query="hooks")]),
        completion(),
    ])
    s14.agent_loop([], client, "test")
    assert requests[1]["messages"][-1]["content"].startswith("MCP error: TypeError:")
    assert requests[2]["messages"][-1]["content"].startswith("MCP error: TypeError:")
    assert requests[3]["messages"][-1]["content"] == "[docs] Found 3 results for 'hooks'"


@pytest.mark.parametrize("answer,allowed", [("y", True), ("n", False)])
def test_deployment_confirmation_uses_host_policy_not_server_hints(monkeypatch, answer, allowed):
    s14.connect_mcp("deploy")
    server = s14.mcp_clients["deploy"]
    server.tools[0]["annotations"] = {"readOnlyHint": True}  # cannot override host policy
    trigger = Mock(return_value="deployment triggered")
    monkeypatch.setitem(server._handlers, "trigger", trigger)
    user_input = Mock(return_value=answer)
    monkeypatch.setattr("builtins.input", user_input)
    after = Mock()
    monkeypatch.setitem(s14.HOOKS, "PostToolUse", [after])
    client, requests = fake_client([
        completion([
            tool("status", "mcp__deploy__status", service="web"),
            tool("trigger", "mcp__deploy__trigger", service="web"),
        ]), completion(),
    ])
    s14.agent_loop([], client, "test")
    user_input.assert_called_once()
    assert trigger.call_count == int(allowed)
    assert after.call_count == 1 + int(allowed)
    results = requests[1]["messages"][-2:]
    assert results[0]["content"] == "[deploy] web: running (v1.4.2)"
    assert results[1]["content"] == ("deployment triggered" if allowed else "Permission denied by user")


def test_unknown_host_policy_requires_approval_and_routing_keeps_original_names(monkeypatch):
    server = s14.MCPClient("docs.one")
    version = Mock(return_value="external version")
    server.register([{"name": "get.version", "inputSchema": {"type": "object"},
                      "annotations": {"readOnlyHint": True}}], {"get.version": version})
    s14.mcp_clients[server.name] = server
    s14.connect_mcp("docs")
    s14.connect_mcp("deploy")
    tools, handlers = s14.assemble_tool_pool()
    name = "mcp__docs_one__get_version"
    assert name in {tool["name"] for tool in tools}
    assert s14.mcp_tool_policies[name] == "confirm"
    user_input = Mock(return_value="n")
    monkeypatch.setattr("builtins.input", user_input)
    assert s14.execute_tool(SimpleNamespace(name=name, input={}), handlers) == "Permission denied by user"
    user_input.assert_called_once()
    version.assert_not_called()
    assert handlers[name]() == "external version"
    assert handlers["mcp__docs__get_version"]() == "[docs] API v2.1.0"
    assert handlers["mcp__deploy__status"](service="api") == "[deploy] api: running (v1.4.2)"
    version.assert_called_once_with()


@pytest.mark.parametrize("tools,error", [
    ([{"name": "a.b"}, {"name": "a_b"}], "collision"),
    ([{"name": "x" * 60}], "longer than 64"),
    ([{"name": "invalid", "inputSchema": {"type": "array"}}], "Invalid input schema"),
])
def test_tool_pool_rejects_ambiguous_or_invalid_external_definitions(tools, error):
    server = s14.MCPClient("custom")
    server.register(tools, {tool["name"]: lambda: "result" for tool in tools})
    s14.mcp_clients["custom"] = server
    with pytest.raises(ValueError, match=error):
        s14.assemble_tool_pool()


def test_server_names_cannot_collide_after_normalization():
    for name in ("docs.one", "docs_one"):
        server = s14.MCPClient(name)
        server.register([{"name": "search"}], {"search": lambda: "result"})
        s14.mcp_clients[name] = server
    with pytest.raises(ValueError, match="collision"):
        s14.assemble_tool_pool()


def test_pretool_hook_can_block_connected_tool(monkeypatch):
    s14.connect_mcp("docs")
    handler = Mock()
    monkeypatch.setitem(s14.mcp_clients["docs"]._handlers, "search", handler)
    monkeypatch.setitem(s14.HOOKS, "PreToolUse", [lambda block: "Blocked by hook"])
    after = Mock()
    monkeypatch.setitem(s14.HOOKS, "PostToolUse", [after])
    _, handlers = s14.assemble_tool_pool()
    block = SimpleNamespace(name="mcp__docs__search", input={"query": "hooks"})
    assert s14.execute_tool(block, handlers) == "Blocked by hook"
    handler.assert_not_called()
    after.assert_not_called()


def test_api_failure_and_truncated_calls_end_with_explicit_error(monkeypatch):
    write = Mock()
    monkeypatch.setitem(s14.BUILTIN_HANDLERS, "write_file", write)
    stop = Mock()
    monkeypatch.setitem(s14.HOOKS, "Stop", [stop])
    for failure in (RuntimeError("model unavailable"), completion(
        [tool("truncated", "write_file", path="a.txt", content="hello")], reason="length",
    )):
        client = Mock()
        if isinstance(failure, Exception):
            client.chat.completions.create.side_effect = failure
        else:
            client.chat.completions.create.return_value = failure
        messages = [{"role": "user", "content": "Write a file"}]
        s14.agent_loop(messages, client, "test")
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"].startswith("[Error] RuntimeError:")
        assert len(messages) == 2
    write.assert_not_called()
    assert stop.call_count == 2


def test_import_and_cli_quit_need_no_model(tmp_path):
    env = dict(os.environ, OPENAI_API_KEY="test-key", MODEL_ID="test-model",
               OPENAI_BASE_URL="https://example.invalid/v1")
    script = Path(s14.__file__).resolve()
    for args in (["-c", "from experiments import s14_mcp_plugin"], [str(script)]):
        result = subprocess.run([sys.executable, *args], input="q\n", text=True,
                                capture_output=True, env=env, timeout=15,
                                cwd=script.parents[1] if args[0] == "-c" else tmp_path)
        assert result.returncode == 0, result.stderr
        assert not result.stderr
