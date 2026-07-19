"""Chunk A stage-2 (gateway service) tests — multi-provider phase-2.

Covers:
  * routing registry: mint/resolve, per-session reuse, revoke, unknown/None → None
    (S2 / G4);
  * spawn-env branch: openai/chatgpt providers → loopback gateway base URL + a
    minted routing token; openrouter stays on the direct path (S3);
  * the module-level SSE translation generator, driven by a STUBBED upstream
    event source — never through ASGITransport, which buffers infinite bodies and
    deadlocks pytest (S7);
  * SSE line parser (iter_sse_events);
  * F1/G9 passthrough guard: passthrough refuses to run without a real API key and
    never reads a Claude subscription token;
  * finite endpoints (401, count_tokens) via ASGITransport, plus the buffered
    non-streaming /v1/messages path with a stubbed upstream;
  * ONE live integration test gated behind TRANSLATOR_LIVE=1 (skipped otherwise),
    exercising ruling R3's combined resend against the real Codex backend.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
import pytest

from app.core.translator_gateway import routing, upstream
from app.core.translator_gateway.events import (
    FinalReasoningItem,
    FinalTextItem,
    FinalToolCallItem,
    NormalizedResponse,
    NormalizedUsage,
)
from app.core.translator_gateway.router import (
    _messages_response_body,
    anthropic_sse_from_events,
)
from app.core.translator_gateway.service import build_gateway_app


# ─── helpers ─────────────────────────────────────────────────────────────────

async def _async_source(
    events: List[Tuple[str, Dict[str, Any]]],
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    for name, payload in events:
        yield name, payload


async def _async_lines(text: str) -> AsyncIterator[str]:
    for line in text.split("\n"):
        yield line


def _canned_text_turn() -> List[Tuple[str, Dict[str, Any]]]:
    return [
        ("response.created", {"response": {"id": "resp_1", "model": "gpt-x"}}),
        ("response.output_item.added", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "Hello"}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": " world"}),
        (
            "response.output_item.done",
            {
                "item": {
                    "id": "msg_1",
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            },
        ),
        (
            "response.completed",
            {
                "response": {
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    "status": "completed",
                }
            },
        ),
    ]


def _parse_sse_bytes(chunks: List[bytes]) -> List[Tuple[str, Dict[str, Any]]]:
    """Parse Anthropic SSE bytes back into (event, data) for assertions."""
    text = b"".join(chunks).decode("utf-8")
    out: List[Tuple[str, Dict[str, Any]]] = []
    event = None
    for block in text.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if event is not None and data is not None:
            out.append((event, data))
    return out


# ─── routing registry (S2 / G4) ──────────────────────────────────────────────

class TestRouting:
    def setup_method(self):
        routing.get_registry().clear()

    def test_mint_and_resolve(self):
        token = routing.mint_session_token("sess", "prov", "acct")
        binding = routing.resolve(token)
        assert binding is not None
        assert binding.session_id == "sess"
        assert binding.provider_id == "prov"
        assert binding.provider_account_id == "acct"

    def test_token_is_opaque_and_long(self):
        token = routing.mint_session_token("s", "p")
        # secrets.token_urlsafe(32) → ~43 url-safe chars; never the raw ids.
        assert len(token) >= 40
        assert "prov" not in token and "sess" not in token

    def test_same_session_reuses_token(self):
        a = routing.mint_session_token("sess", "prov", "acct")
        b = routing.mint_session_token("sess", "prov", "acct")
        assert a == b

    def test_session_provider_change_drops_stale_token(self):
        a = routing.mint_session_token("sess", "prov1")
        b = routing.mint_session_token("sess", "prov2")
        assert a != b
        assert routing.resolve(a) is None  # stale token no longer routes
        assert routing.resolve(b).provider_id == "prov2"

    def test_sessionless_mints_fresh_each_time(self):
        a = routing.mint_session_token(None, "prov")
        b = routing.mint_session_token(None, "prov")
        assert a != b  # no session key to reuse on
        assert routing.resolve(a).provider_id == "prov"

    def test_unknown_and_missing_token_resolve_none(self):
        assert routing.resolve(None) is None
        assert routing.resolve("") is None
        assert routing.resolve("not-a-real-token") is None

    def test_revoke_session(self):
        token = routing.mint_session_token("sess", "prov")
        routing.get_registry().revoke_session("sess")
        assert routing.resolve(token) is None


# ─── spawn-env branch (S3) ───────────────────────────────────────────────────

class TestSpawnEnvBranch:
    def setup_method(self):
        routing.get_registry().clear()

    def _apply(self, mock_db, provider_type, *, session_id="sess-A"):
        from app.core import encryption
        from app.core.environment import apply_provider_env
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="gw",
            name="Codex" if provider_type == "chatgpt" else "OpenAI",
            provider_type=provider_type,
            base_url="",  # Codex/OpenAI have no provider base URL
            auth_token=None,  # credentials live in codex blobs / gateway, not here
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        env: Dict[str, str] = {}
        apply_provider_env(env, "gw", "gpt-5-codex", session_id=session_id)
        return env

    def test_chatgpt_routes_to_loopback_gateway(self, mock_db):
        from app.core.config import settings

        env = self._apply(mock_db, "chatgpt")
        assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{settings.translator_gateway_port}"
        # The auth token is the minted opaque routing token, NOT a real secret.
        token = env["ANTHROPIC_AUTH_TOKEN"]
        binding = routing.resolve(token)
        assert binding is not None and binding.provider_id == "gw"
        assert binding.session_id == "sess-A"
        # Guards preserved exactly.
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"] == "1"
        # R4 small/fast + subagent still set from the curated model.
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "gpt-5-codex"
        assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "gpt-5-codex"

    def test_openai_routes_to_loopback_gateway(self, mock_db):
        from app.core.config import settings

        env = self._apply(mock_db, "openai")
        assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{settings.translator_gateway_port}"
        assert routing.resolve(env["ANTHROPIC_AUTH_TOKEN"]).provider_id == "gw"

    def test_openrouter_keeps_direct_path_unchanged(self, mock_db):
        """openrouter/custom must NOT be rewritten to the gateway."""
        from app.core import encryption
        from app.core.environment import apply_provider_env
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="or",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token=encryption.encrypt_value("or-secret"),
            models=[{"id": "x/y", "label": "y"}],
        )
        env: Dict[str, str] = {}
        apply_provider_env(env, "or", "x/y", session_id="sess-B")
        assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "or-secret"  # direct decrypted key
        # No routing token was minted for a direct provider.
        assert routing.resolve("or-secret") is None


# ─── SSE translation generator (S7 — stubbed source, no ASGITransport) ───────

class TestSSEGenerator:
    async def test_text_turn_translates_to_anthropic_sse(self):
        chunks = [
            chunk
            async for chunk in anthropic_sse_from_events(
                _async_source(_canned_text_turn()), request_model="claude-x"
            )
        ]
        events = _parse_sse_bytes(chunks)
        names = [name for name, _ in events]
        assert names[0] == "message_start"
        assert "content_block_start" in names
        assert "content_block_delta" in names
        assert "message_delta" in names
        assert names[-1] == "message_stop"
        # The text deltas made it through byte-for-byte.
        text = "".join(
            d["delta"]["text"]
            for n, d in events
            if n == "content_block_delta" and d["delta"].get("type") == "text_delta"
        )
        assert text == "Hello world"
        # Authoritative usage landed in message_delta.
        (msg_delta,) = [d for n, d in events if n == "message_delta"]
        assert msg_delta["usage"]["input_tokens"] == 10
        assert msg_delta["usage"]["output_tokens"] == 5

    async def test_upstream_error_mid_stream_emits_error_frame(self):
        async def _boom() -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
            yield ("response.created", {"response": {"id": "r", "model": "m"}})
            raise upstream.UpstreamError(429, "rate limited")

        chunks = [
            chunk
            async for chunk in anthropic_sse_from_events(_boom(), request_model="m")
        ]
        events = _parse_sse_bytes(chunks)
        assert events[-1][0] == "error"
        assert events[-1][1]["error"]["type"] == "rate_limit_error"


# ─── SSE line parser ─────────────────────────────────────────────────────────

class TestIterSSEEvents:
    async def test_parses_event_and_data(self):
        raw = (
            "event: response.output_text.delta\n"
            'data: {"item_id":"m","delta":"hi"}\n'
            "\n"
            ": heartbeat\n"
            "event: response.completed\n"
            'data: {"response":{"usage":{}}}\n'
            "\n"
            "data: [DONE]\n"
            "\n"
        )
        out = [ev async for ev in upstream.iter_sse_events(_async_lines(raw))]
        assert out[0][0] == "response.output_text.delta"
        assert out[0][1]["delta"] == "hi"
        assert out[1][0] == "response.completed"
        assert len(out) == 2  # [DONE] and heartbeat skipped

    async def test_falls_back_to_payload_type(self):
        raw = 'data: {"type":"response.created","response":{}}\n\n'
        out = [ev async for ev in upstream.iter_sse_events(_async_lines(raw))]
        assert out[0][0] == "response.created"


# ─── F1/G9 passthrough guard (S5) ────────────────────────────────────────────

class TestPassthroughGuard:
    def test_refuses_without_api_key(self):
        with pytest.raises(upstream.UpstreamAuthError):
            upstream.require_passthrough_key({"provider_type": "anthropic", "auth_token": None})
        with pytest.raises(upstream.UpstreamAuthError):
            upstream.require_passthrough_key({})

    def test_returns_plaintext_key(self):
        assert upstream.require_passthrough_key({"auth_token": "sk-ant-plain"}) == "sk-ant-plain"

    def test_returns_decrypted_key(self, mock_db):
        from app.core import encryption
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        blob = encryption.encrypt_value("sk-ant-secret")
        assert upstream.require_passthrough_key({"auth_token": blob}) == "sk-ant-secret"


# ─── buffered assembly (non-streaming) ───────────────────────────────────────

class TestBufferedAssembly:
    def test_messages_response_body_shape(self):
        final = NormalizedResponse(
            items=[
                FinalReasoningItem(encrypted_content="ec", summary=[{"type": "summary_text", "text": "thinking..."}], signature="sig"),
                FinalTextItem(text="answer"),
                FinalToolCallItem(call_id="call_1", name="get_weather", arguments='{"city":"SF"}'),
            ],
            usage=NormalizedUsage(input_tokens=7, output_tokens=3),
            stop_reason="tool_use",
            model="gpt-x",
            message_id="msg_resp_1",
        )
        body = _messages_response_body(final, "claude-x")
        assert body["id"] == "msg_resp_1"
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["stop_reason"] == "tool_use"
        assert body["model"] == "gpt-x"
        kinds = [b["type"] for b in body["content"]]
        assert kinds == ["thinking", "text", "tool_use"]
        assert body["content"][0]["signature"] == "sig"
        assert body["content"][0]["thinking"] == "thinking..."
        tool = body["content"][2]
        assert tool["id"] == "call_1" and tool["name"] == "get_weather"
        assert tool["input"] == {"city": "SF"}
        assert body["usage"]["input_tokens"] == 7


# ─── finite endpoints via ASGITransport (401 / count_tokens / buffered) ──────

async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_gateway_app())
    return httpx.AsyncClient(transport=transport, base_url="http://gw")


class TestEndpoints:
    def setup_method(self):
        routing.get_registry().clear()

    async def test_missing_token_401_anthropic_shape(self):
        async with await _client() as c:
            resp = await c.post("/v1/messages", json={"model": "m", "messages": []})
        assert resp.status_code == 401
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "authentication_error"

    async def test_bad_token_401(self):
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": "Bearer nope"},
                json={"model": "m", "messages": []},
            )
        assert resp.status_code == 401

    async def test_count_tokens_requires_token(self):
        async with await _client() as c:
            resp = await c.post("/v1/messages/count_tokens", json={"messages": []})
        assert resp.status_code == 401

    async def test_count_tokens_ok(self):
        token = routing.mint_session_token("sess", "prov")
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages/count_tokens",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hello there"}],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["input_tokens"], int)
        assert body["input_tokens"] > 0

    async def test_buffered_messages_via_stubbed_upstream(self, monkeypatch):
        """Non-streaming /v1/messages assembles a complete Messages response from
        a stubbed upstream (finite body → safe through ASGITransport)."""
        token = routing.mint_session_token("sess", "prov")

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            assert mode == "codex"
            return _async_source(_canned_text_turn())

        monkeypatch.setattr("app.core.translator_gateway.router._load_provider", _fake_provider)
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "claude-x", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message"
        assert body["content"][0]["type"] == "text"
        assert body["content"][0]["text"] == "Hello world"
        assert body["usage"]["input_tokens"] == 10


# ─── live integration (gated; real Codex backend; ruling R3 combined resend) ─

def _load_live_codex_provider() -> Optional[Dict[str, Any]]:
    """Find an enabled Codex provider with usable credentials, or None.

    Reads only model-provider credentials (never claude_accounts — F1/G9). The
    encryption key must already be available in the running process for the
    stored Codex blobs to decrypt; otherwise we treat creds as unavailable.
    """
    try:
        from app.core import encryption
        from app.db import database as db

        if not encryption.is_encryption_ready():
            return None
        for row in db.get_enabled_model_providers() or []:
            if row.get("auth_mode") != "chatgpt":
                continue
            tokens = db.get_codex_tokens(row["id"])
            if not tokens or tokens.get("needs_reauth"):
                continue
            enc = tokens.get("codex_access_token")
            if not enc:
                continue
            try:
                if encryption.decrypt_value(enc):
                    return row
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


@pytest.mark.skipif(
    os.getenv("TRANSLATOR_LIVE") != "1",
    reason="live Codex integration test (set TRANSLATOR_LIVE=1 with a connected ChatGPT provider)",
)
async def test_live_codex_combined_resend():
    """Full loopback-gateway path against the REAL Codex backend, including
    ruling R3: a resent history containing reasoning + function_call +
    function_call_output in one turn must be accepted (HTTP 200 stream)."""
    provider = _load_live_codex_provider()
    if provider is None:
        pytest.skip("no usable Codex provider credentials available")

    from app.core.translator_gateway.service import GatewayServer

    server = GatewayServer()
    await server.start()
    try:
        token = routing.mint_session_token("live-sess", provider["id"], provider.get("codex_account_id"))
        model = provider.get("default_model") or "gpt-5-codex"
        # R3 combined resend: a prior assistant turn that reasoned AND called a
        # tool, followed by the tool result — all resent in one history.
        sig = __import__(
            "app.core.translator_gateway.events", fromlist=["encode_reasoning_signature"]
        ).encode_reasoning_signature("dummy-encrypted", [{"type": "summary_text", "text": "s"}], [])
        anthropic_req = {
            "model": model,
            "max_tokens": 256,
            "stream": True,
            "messages": [
                {"role": "user", "content": "What is the weather in SF?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "s", "signature": sig},
                        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "SF"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "72F and sunny"}
                    ],
                },
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather for a city",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
        }
        got_message_start = False
        async with httpx.AsyncClient(timeout=60.0) as c:
            async with c.stream(
                "POST",
                f"http://127.0.0.1:{server.port}/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=anthropic_req,
            ) as resp:
                assert resp.status_code == 200, await resp.aread()
                async for line in resp.aiter_lines():
                    if line.startswith("event: message_start"):
                        got_message_start = True
                    if line.startswith("event: message_stop"):
                        break
        assert got_message_start
    finally:
        await server.stop()
