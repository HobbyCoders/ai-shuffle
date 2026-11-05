"""Native gateway regression: real engine -> Messages wire -> tool -> replay."""
import json

import httpx
import pytest

from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, ToolResult, TurnComplete, UserTurn
from fluid.messages import Message, Reasoning, Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.anthropic.serialize import build_request_body as to_request
from fluid.providers.base import ProviderRequest
from fluid.providers.compatible import BASE_URLS
from fluid.providers.pricing import compatible_cost
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry
from scripts.devloop.fake_wire import FakeProviderServer, Reply


def stream(tool_call=False):
    blocks = [("thinking", {"thinking": ""}, "thinking_delta", {"thinking": "need a tool"})]
    if tool_call:
        blocks.append(("tool_use", {"id": "call-1", "name": "mcp__demo__hello", "input": {}},
                       "input_json_delta", {"partial_json": '{"name":"world"}'}))
    else:
        blocks.append(("text", {"text": ""}, "text_delta", {"text": "Hello world"}))
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}}]
    for i, (kind, block, delta_kind, delta) in enumerate(blocks):
        events.extend([
            {"type": "content_block_start", "index": i, "content_block": {"type": kind, **block}},
            {"type": "content_block_delta", "index": i, "delta": {"type": delta_kind, **delta}},
            {"type": "content_block_stop", "index": i},
        ])
    events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool_call else "end_turn"},
                    "usage": {"output_tokens": 4}}, {"type": "message_stop"}])
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


@pytest.mark.parametrize("provider,mode", BASE_URLS)
async def test_native_engine_tools_replay_and_stable_session(provider, mode):
    # Actual local TCP/SSE, not a mocked provider client. Keep each vendor's
    # documented path while redirecting only the origin to the fake server.
    path = httpx.URL(BASE_URLS[provider, mode]).path + "/v1/messages"
    server = FakeProviderServer(*[Reply(frames=[stream(tool_call=i % 2 == 0).encode()]) for i in range(4)])

    @tool("hello", "Say hello", {"type": "object", "properties": {"name": {"type": "string"}}})
    async def hello(args):
        return {"content": [{"type": "text", "text": "Hello " + args["name"]}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("demo", [hello]))
    async with httpx.AsyncClient() as http:
        client = AnthropicClient(api_key="test-gateway-secret", base_url=server.url + path,
                                 compatible_provider=provider, user_agent="ai-shuffle/test", http_client=http)
        loop = NativeLoop(provider=client, store=MemoryStore(), model="test-model", registry=registry,
                          permissions=PermissionEngine("never_ask"))
        try:
            for _ in range(2):
                await loop.submit(UserTurn(content="Use hello"))
                await loop.wait_turn()
            events = loop.bus.log(loop.session_id)
            assert len([e for e in events if isinstance(e, TurnComplete)]) == 2
            assert len([e for e in events if isinstance(e, ToolResult)]) == 2
            assert not [e for e in events if isinstance(e, ErrorEvent)]
            calls = [(request, request.body) for request in server.requests]
            assert len(calls) == 4
            for request, body in calls:
                assert request.path == path
                headers = {k.lower(): v for k, v in request.headers.items()}
                assert headers["user-agent"] == "ai-shuffle/test"
                assert headers["authorization"] == "Bearer test-gateway-secret"
                assert headers["x-api-key"] == "test-gateway-secret"
                if provider == "opencode":
                    assert headers["x-opencode-session"] == loop.session_id
                assert "cache_control" not in json.dumps(body)
                assert not any(k in body for k in ("betas", "context_management", "mcp_servers"))
            replay = calls[1][1]["messages"]
            assert any(b["type"] == "tool_result" for m in replay for b in m["content"])
            thinking = [b for m in replay for b in m["content"] if b["type"] == "thinking"]
            assert thinking == [{"type": "thinking", "thinking": "need a tool"}]
        finally:
            await loop.aclose()
            server.close()


@pytest.mark.parametrize("provider", ["kimi", "minimax", "opencode"])
def test_claude_features_do_not_leak(provider):
    request = ProviderRequest(model="claude-opus-4-7", messages=[Message(role="user", content=[Text("Hi")])],
                              cache_breakpoints=4, effort="unsupported", thinking={"type": "enabled", "budget_tokens": 9999},
                              extra={"betas": ["bad"], "mcp_servers": ["bad"], "context_management": {"bad": True}},
                              tools=[{"type": "web_search_20250305", "name": "web_search"},
                                     {"name": "hello", "input_schema": {"type": "object"}}])
    body = to_request(request, compatible_provider=provider)
    assert body["max_tokens"] == 8192
    assert body["tools"] == [{"name": "hello", "input_schema": {"type": "object"}}]
    assert "thinking" not in body and "betas" not in body
    assert "cache_control" not in json.dumps(body)


def test_anthropic_still_rejects_unsigned_thinking():
    thought = Reasoning(origin="anthropic", model="claude-sonnet-4-5", raw={"type": "thinking", "thinking": "x"})
    request = ProviderRequest(model="claude-sonnet-4-5", messages=[Message(role="assistant", content=[thought, Text("Hi")])])
    assert to_request(request)["messages"][0]["content"] == [{"type": "text", "text": "Hi"}]


@pytest.mark.parametrize("provider,status", [("kimi", 401), ("minimax", 429), ("opencode", 503)])
async def test_errors(provider, status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json={
            "error": {"type": "authentication_error" if status == 401 else "overloaded_error", "message": "Refused fake"}}))) as http:
        client = AnthropicClient(api_key="fake", compatible_provider=provider, user_agent="ai-shuffle/test",
                                 http_client=http, retry=RetryPolicy(max_retries=0))
        chunks = [c async for c in client.stream(ProviderRequest(model="test", messages=[], session_id="same-session"))]
        assert chunks[-1].kind == "error"
        assert chunks[-1].payload["status"] == status
        assert "fake" not in json.dumps(chunks[-1].payload)


def test_scoped_pricing_never_inherits_claude_rates():
    usage = {"input_tokens": 100, "output_tokens": 20}
    assert compatible_cost("opencode", "claude-opus-4-7", usage) is None
    assert compatible_cost("kimi", "kimi-for-coding", usage) is None
    assert compatible_cost("minimax", "MiniMax-M3", usage) > 0
    assert compatible_cost("minimax", "unknown", usage) is None
    # OpenCode Claude IDs use Console's own sourced prices, never direct Claude.
    assert compatible_cost("opencode", "claude-opus-5-5", usage) == pytest.approx(0.0008)
    assert compatible_cost("opencode", "kimi-k3.1-pro", usage) is None
    for model, threshold in (("gpt-6.1-sol", 272_000), ("gpt-6-luna", 272_000), ("grok-4.7", 200_000)):
        low = compatible_cost("opencode", model, {"input_tokens": threshold})
        high = compatible_cost("opencode", model, {"input_tokens": threshold + 1})
        assert high > low * 1.99
    opencode_plan = AnthropicClient(api_key="fake", compatible_provider="opencode", plan_key=True,
                                    user_agent="ai-shuffle/test")
    assert opencode_plan.estimate_cost("claude-opus-5-5", usage) is None
    plan = AnthropicClient(api_key="fake", compatible_provider="minimax", plan_key=True, user_agent="ai-shuffle/test")
    assert plan.estimate_cost("MiniMax-M3", usage) is None


@pytest.mark.parametrize("kind,url", [
    ("chatgpt", "https://chatgpt.com/backend-api/codex/responses"),
    ("xai", "https://api.x.ai/v1/responses"),
])
async def test_existing_responses_headers_and_dialect_unchanged(kind, url):
    from fluid.providers.openai_responses.client import OpenAIResponsesClient
    from scripts.devloop.fake_wire import Responses

    class Auth:
        account_id = "test-account"
        async def get_access_token(self):
            return "test-existing-key"
        async def on_401(self):
            raise AssertionError("unexpected refresh")

    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"".join(Responses.text("Hello")),
                              headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAIResponsesClient(base_url=url, auth=Auth() if kind == "chatgpt" else None,
                                       api_key=None if kind == "chatgpt" else "test-existing-key", http_client=http)
        chunks = [c async for c in client.stream(ProviderRequest(model="test-model", messages=[], session_id="unused"))]
    assert any(c.kind == "text_delta" for c in chunks)
    request = seen[0]
    assert str(request.url) == url
    assert request.headers["authorization"] == "Bearer test-existing-key"
    assert "x-opencode-session" not in request.headers and "x-api-key" not in request.headers
    assert not request.headers["user-agent"].startswith("ai-shuffle/")
    body = json.loads(request.content)
    assert body["model"] == "test-model" and "input" in body and "messages" not in body
    assert "session_id" not in body


async def test_real_anthropic_headers_remain_unmodified():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AnthropicClient(api_key="anthropic-api-test", http_client=http)
        _ = [c async for c in client.stream(ProviderRequest(model="claude-sonnet-4-5", messages=[]))]
    assert seen[0].headers["x-api-key"] == "anthropic-api-test"
    assert "authorization" not in seen[0].headers
    assert "x-opencode-session" not in seen[0].headers
    assert not seen[0].headers["user-agent"].startswith("ai-shuffle/")
    assert client.caps.signed_thinking and client.caps.server_tools


async def test_opencode_checkpoint_uses_same_conversation_header():
    from fluid.context.checkpoint_summary import build_checkpoint
    from fluid.providers.base import WireState

    seen = []
    def handler(request):
        seen.append(request.headers["x-opencode-session"])
        return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AnthropicClient(api_key="fake", compatible_provider="opencode", user_agent="ai-shuffle/test", http_client=http)
        await build_checkpoint(client, model="test", text="history", max_tokens=50,
                               wire_state=WireState(session_id="conversation-1", role="checkpoint"))
        _ = [chunk async for chunk in client.stream(ProviderRequest(model="test", messages=[], session_id="conversation-1"))]
    assert seen == ["conversation-1", "conversation-1"]


@pytest.mark.parametrize("provider,mode", BASE_URLS)
def test_compatible_configuration_round_trip_keeps_destination_and_profile(monkeypatch, provider, mode):
    from fluid.config.loader import _build_provider_client
    from fluid.config.model import HarnessConfig

    monkeypatch.setenv("TEST_GATEWAY_KEY", "fake-key")
    config = HarnessConfig.from_dict({"providers": {"gateway": {
        "kind": "anthropic", "compatible_provider": provider, "auth_mode": mode,
        "api_key_env": "TEST_GATEWAY_KEY", "user_agent": "ai-shuffle/test"}}})
    restored = HarnessConfig.from_dict(json.loads(json.dumps(config.to_dict())))
    client = _build_provider_client(restored, "gateway")
    assert client._base_url == BASE_URLS[provider, mode] + "/v1/messages"
    assert client.name == provider and client._api_key == "fake-key"
    assert client._plan_key == (mode == "plan_key")
    assert client._user_agent == "ai-shuffle/test"


@pytest.mark.parametrize("provider", ["kimi", "minimax", "opencode"])
def test_cache_annotations_strip_protocol_only_and_keep_application_data(provider):
    from fluid.messages import ToolUseBlock, ToolResultBlock

    marker = {"type": "ephemeral"}
    schema = {"type": "object", "properties": {"cache_control": {"type": "string"}},
              "required": ["cache_control"]}
    arguments = {"cache_control": "keep", "nested": {"cache_control": "also keep"}}
    result = json.dumps(arguments)
    request = ProviderRequest(model="test-model", system="Stable system", cache_breakpoints=3,
        tools=[{"name": "hello", "input_schema": schema, "cache_control": marker}],
        messages=[Message(role="assistant", content=[ToolUseBlock(id="t", name="hello", input=arguments)]),
                  Message(role="user", content=[ToolResultBlock(tool_use_id="t", content=[Text(result)])])])
    anthropic_before = json.dumps(to_request(request))
    body = to_request(request, compatible_provider=provider)
    assert body["tools"] == [{"name": "hello", "input_schema": schema}]
    assert body["messages"][0]["content"][0]["input"] == arguments
    assert body["messages"][1]["content"][0]["content"] == [{"type": "text", "text": result}]
    assert "cache_control" not in body["system"][0]
    assert all("cache_control" not in block for msg in body["messages"] for block in msg["content"])
    assert json.dumps(to_request(request)) == anthropic_before
    assert arguments["cache_control"] == "keep" and "cache_control" in schema["properties"]
    # Raw protocol content supplied by a caller has the same boundary: strip
    # annotations on result blocks, not a nested application value.
    raw = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
        "cache_control": marker, "content": [{"type": "text", "text": result,
        "cache_control": marker, "data": arguments}]}]}]
    request.extra = {"messages": raw}
    anthropic_before = json.dumps(to_request(request))
    body = to_request(request, compatible_provider=provider)
    result_block = body["messages"][0]["content"][0]
    assert "cache_control" not in result_block and "cache_control" not in result_block["content"][0]
    assert result_block["content"][0]["data"] == arguments
    assert json.dumps(to_request(request)) == anthropic_before


@pytest.mark.parametrize("provider", ["kimi", "minimax", "opencode"])
async def test_native_chat_never_forwards_credentials_on_redirect(provider):
    calls = []
    def redirect(request):
        calls.append(request)
        if request.url.host == "foreign.example":
            return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
        return httpx.Response(307, headers={"location": "https://foreign.example/v1/messages"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as http:
        client = AnthropicClient(api_key="fake", compatible_provider=provider, user_agent="ai-shuffle/test",
                                 http_client=http, retry=RetryPolicy(max_retries=0))
        chunks = [c async for c in client.stream(ProviderRequest(model="test", messages=[], session_id="conversation"))]
    assert chunks[-1].kind == "error"
    assert len(calls) == 1 and calls[0].url.host != "foreign.example"


@pytest.mark.parametrize("binding", ["scope", "routing", "host", "engine"])
async def test_opencode_learner_keeps_bound_or_scope_identity_across_passes(tmp_path, binding):
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.skills import SkillStore
    from fluid.providers.base import WireState

    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
    store = SkillStore(tmp_path / "memory", owner="test-owner")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AnthropicClient(api_key="fake", compatible_provider="opencode", user_agent="ai-shuffle/test", http_client=http)
        for _ in range(2):
            synth = Synthesizer(client, "test", store, project="project-scope",
                                session_id="conversation-1" if binding == "host" else None)
            loop = None
            if binding == "routing":
                synth.bind_routing(lambda: ({"prompt_cache_key": "responses-only"}, WireState(
                    session_id="conversation-1", role="learner")))
            elif binding == "engine":
                from fluid.codemode.journal import ToolJournal

                loop = NativeLoop(provider=client, store=MemoryStore(), model="test",
                                  session_id="conversation-1", journal=ToolJournal(store, synth))
                await loop._ensure_session()
            try:
                await synth._complete("Synthesize this", reserve=0)
            finally:
                if loop is not None:
                    await loop.aclose()
    ids = [r.headers["x-opencode-session"] for r in seen]
    assert ids[0] == ids[1]
    if binding == "scope":
        assert ids[0].startswith("learner:")
    else:
        assert ids[0] == "conversation-1"
    assert all("prompt_cache_key" not in json.loads(r.content) for r in seen)


async def test_opencode_harvest_uses_stable_project_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fluid.cli.evolve import draft_anchors
    from fluid.config import loader
    from fluid.store.base import project_root_for

    config = SimpleNamespace(cwd=tmp_path, default_provider="p", continual_model=None, model_for=lambda _: "test")
    project = project_root_for(tmp_path)
    store = SimpleNamespace(
        recent_sessions=AsyncMock(return_value=[SimpleNamespace(project_root=project, session_id="s")]),
        session_slice=AsyncMock(return_value=SimpleNamespace(messages=[SimpleNamespace(
            role="user", tools=[], text="completed migration")]))
    )
    seen = []
    def respond(request):
        seen.append(request.headers["x-opencode-session"])
        return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AnthropicClient(api_key="fake", compatible_provider="opencode", user_agent="ai-shuffle/test", http_client=http)
        monkeypatch.setattr(loader, "_build_provider_client", lambda *_: client)
        for _ in range(2):
            await draft_anchors(config, store)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0].startswith("harvest:") and project not in seen[0]


async def test_text_tools_wrapper_preserves_opencode_conversation_identity():
    from fluid.providers.base import WireState
    from fluid.providers.local.text_tools import TextToolsAdapter

    seen = []
    def respond(request):
        seen.append(request.headers["x-opencode-session"])
        return httpx.Response(200, text=stream(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AnthropicClient(api_key="fake", compatible_provider="opencode", user_agent="ai-shuffle/test", http_client=http)
        wrapped = TextToolsAdapter(client)
        wire = WireState(session_id="conversation-1", role="main")
        request = ProviderRequest(model="test", messages=[], session_id="conversation-1", wire_state=wire)
        assert wrapped._prepare(request).wire_state is wire
        _ = [chunk async for chunk in wrapped.stream(request)]
    assert seen == ["conversation-1"]
