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
    LegRoute,
    _messages_response_body,
    anthropic_sse_from_events,
    resolve_leg_model,
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

    # ── Chunk F/S1: re-mint drops the stale token on a MAIN provider change ───

    def test_remint_main_provider_change_drops_stale_even_with_leg(self):
        """A change to the MAIN provider still drops the stale token (regression:
        the leg-update path must not swallow a genuine main-route change)."""
        a = routing.mint_session_token(
            "sess", "prov1", "acct", legs=(("m", "sub", None),),
        )
        b = routing.mint_session_token(
            "sess", "prov2", "acct", legs=(("m", "sub", None),),
        )
        assert a != b
        assert routing.resolve(a) is None
        assert routing.resolve(b).provider_id == "prov2"

    # ── S1: the N-leg map on the binding (successor to the singular fields) ───

    def test_mint_carries_legs(self):
        legs = (("m1", "provA", None), ("m2", "provB", None))
        token = routing.mint_session_token("sess", "main-prov", "acct", legs=legs)
        b = routing.resolve(token)
        assert b.provider_id == "main-prov"
        assert b.legs == legs

    def test_mint_without_legs_leaves_legs_empty(self):
        token = routing.mint_session_token("sess", "main-prov")
        b = routing.resolve(token)
        assert b.legs == ()

    def test_remint_same_main_different_legs_keeps_token_refreshes(self):
        """Re-minting the SAME session whose main route (provider + account) is
        unchanged but whose leg MAP differs keeps the token and refreshes the
        map in place (same dedup branch the singular leg fields use)."""
        l1 = (("m1", "provA", None),)
        l2 = (("m2", "provB", None),)
        a = routing.mint_session_token("sess", "main-prov", "acct", legs=l1)
        b = routing.mint_session_token("sess", "main-prov", "acct", legs=l2)
        assert a == b  # same token
        assert routing.resolve(b).legs == l2

    def test_remint_identical_legs_reuses_token(self):
        legs = (("m1", "provA", None),)
        a = routing.mint_session_token("sess", "main-prov", "acct", legs=legs)
        b = routing.mint_session_token("sess", "main-prov", "acct", legs=legs)
        assert a == b

    # ── S8-R remap: the binding carries the main provider's real model ─────────

    def test_mint_carries_main_upstream_model(self):
        token = routing.mint_session_token(
            "sess", "main-prov", "acct", main_upstream_model="gpt-5.5",
        )
        b = routing.resolve(token)
        assert b.main_upstream_model == "gpt-5.5"

    def test_mint_without_main_upstream_model_is_none(self):
        token = routing.mint_session_token("sess", "main-prov")
        assert routing.resolve(token).main_upstream_model is None

    def test_remint_same_main_different_main_upstream_model_keeps_token_refreshes(self):
        """Re-minting the SAME session whose main route (provider + account) is
        unchanged but whose ``main_upstream_model`` differs keeps the token and
        refreshes the binding in place (a within-provider composer model switch)."""
        a = routing.mint_session_token(
            "sess", "main-prov", "acct", main_upstream_model="gpt-5.5",
        )
        b = routing.mint_session_token(
            "sess", "main-prov", "acct", main_upstream_model="gpt-6",
        )
        assert a == b  # same token
        assert routing.resolve(b).main_upstream_model == "gpt-6"


# ─── Chunk F: leg resolution (requested model → served leg) ──────────────────

class TestResolveLegModel:
    def _binding(self, *, legs=(), **kw):
        return routing.RouteBinding(
            session_id="s", provider_id="main-prov", legs=legs, **kw
        )

    def test_other_model_stays_on_main_leg(self):
        binding = self._binding(legs=(("gpt-5-codex", "sub-prov", None),))
        leg = resolve_leg_model(binding, "claude-sonnet-4")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="claude-sonnet-4")

    def test_no_legs_passthrough(self):
        binding = self._binding()
        leg = resolve_leg_model(binding, "any-model")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="any-model")

    def test_none_requested_model_stays_main(self):
        binding = self._binding(legs=(("gpt-5-codex", "sub-prov", None),))
        leg = resolve_leg_model(binding, None)
        assert leg.provider_id == "main-prov"
        assert leg.upstream_model is None

    # ── S1: the N-leg map path ────────────────────────────────────────────────

    def test_legs_map_single_entry_routes(self):
        binding = self._binding(legs=(("m1", "provA", None),))
        leg = resolve_leg_model(binding, "m1")
        assert leg == LegRoute(provider_id="provA", upstream_model="m1")

    def test_legs_map_two_entries_route_independently(self):
        binding = self._binding(legs=(("m1", "provA", None), ("m2", "provB", None)))
        assert resolve_leg_model(binding, "m1") == LegRoute(
            provider_id="provA", upstream_model="m1"
        )
        assert resolve_leg_model(binding, "m2") == LegRoute(
            provider_id="provB", upstream_model="m2"
        )

    def test_legs_map_no_match_stays_main(self):
        binding = self._binding(legs=(("m1", "provA", None), ("m2", "provB", None)))
        leg = resolve_leg_model(binding, "other")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="other")

    def test_legs_empty_map_stays_main(self):
        binding = self._binding(legs=())
        leg = resolve_leg_model(binding, "m1")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="m1")

    def test_legs_collision_first_entry_wins(self):
        """Two entries with the SAME model id but different providers: the FIRST
        tuple entry wins deterministically. This documents the gateway's
        order-scan guarantee that S5 collision-resolution relies on."""
        binding = self._binding(legs=(("m", "provFIRST", None), ("m", "provSECOND", None)))
        leg = resolve_leg_model(binding, "m")
        assert leg == LegRoute(provider_id="provFIRST", upstream_model="m")

    # ── S8-R remap: stray claude-*/alias id on a translated main → main model ──

    def test_claude_id_remaps_to_main_model_on_translated_main(self):
        binding = self._binding(main_upstream_model="gpt-5.5")
        leg = resolve_leg_model(binding, "claude-3-5-haiku-20241022")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="gpt-5.5")

    def test_bare_alias_remaps_to_main_model(self):
        binding = self._binding(main_upstream_model="gpt-5.5")
        leg = resolve_leg_model(binding, "haiku")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="gpt-5.5")

    def test_claude_id_matching_a_leg_wins_no_remap(self):
        """A claude-* id that EXACTLY matches a leg routes to that leg — the leg
        scan runs before the remap, so the leg wins."""
        binding = self._binding(
            legs=(("claude-sonnet-4", "sub-prov", None),),
            main_upstream_model="gpt-5.5",
        )
        leg = resolve_leg_model(binding, "claude-sonnet-4")
        assert leg == LegRoute(provider_id="sub-prov", upstream_model="claude-sonnet-4")

    def test_claude_id_no_main_upstream_model_passes_through(self):
        """No ``main_upstream_model`` (anthropic-passthrough main) → old behavior:
        the claude-* id passes through verbatim to the main leg."""
        binding = self._binding()
        leg = resolve_leg_model(binding, "claude-sonnet-4")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="claude-sonnet-4")

    def test_non_claude_unknown_id_not_remapped(self):
        """A non-claude, non-alias unknown id (e.g. a stray OpenAI id) is NOT
        remapped even on a translated main — it passes through verbatim."""
        binding = self._binding(main_upstream_model="gpt-5.5")
        leg = resolve_leg_model(binding, "gpt-4o")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="gpt-4o")

    def test_requested_equals_main_model_no_remap(self):
        """When the requested id already IS the main upstream model, no remap
        (the guard excludes it) — passes through verbatim."""
        binding = self._binding(main_upstream_model="gpt-5.5")
        leg = resolve_leg_model(binding, "gpt-5.5")
        assert leg == LegRoute(provider_id="main-prov", upstream_model="gpt-5.5")


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
        # R4 small/fast still set from the curated model; S3 no longer emits
        # CLAUDE_CODE_SUBAGENT_MODEL (unstamped Task subagents inherit MAIN).
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "gpt-5-codex"
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env

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

    # ── oversize guard (translated modes only) ────────────────────────────────

    async def test_oversize_request_rejected_400(self, monkeypatch):
        """A body estimated above the provider's context_window → 400 in the
        Anthropic error shape, before any upstream call (finite → ASGI-safe)."""
        token = routing.mint_session_token("sess", "prov")
        streamed: List[str] = []

        async def _fake_provider(provider_id):
            return {
                "id": provider_id, "provider_type": "chatgpt",
                "auth_mode": "chatgpt", "context_window": 5,
            }

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            streamed.append(provider_id)
            return _async_source(_canned_text_turn())

        monkeypatch.setattr("app.core.translator_gateway.router._load_provider", _fake_provider)
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        big = "hello there this is a long message " * 50
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "gpt-5-codex",
                      "messages": [{"role": "user", "content": big}], "stream": False},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert not streamed  # rejected before any upstream dispatch

    async def test_under_window_proceeds(self, monkeypatch):
        """A body under the provider's context_window proceeds to translation and
        the (stubbed) upstream as before."""
        token = routing.mint_session_token("sess", "prov")

        async def _fake_provider(provider_id):
            return {
                "id": provider_id, "provider_type": "chatgpt",
                "auth_mode": "chatgpt", "context_window": 200000,
            }

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            return _async_source(_canned_text_turn())

        monkeypatch.setattr("app.core.translator_gateway.router._load_provider", _fake_provider)
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "gpt-5-codex",
                      "messages": [{"role": "user", "content": "hi"}], "stream": False},
            )
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "Hello world"

    async def test_no_context_window_skips_guard(self, monkeypatch):
        """No context_window on the provider row → guard skipped; even a large
        body proceeds to the (stubbed) upstream (no guessing)."""
        token = routing.mint_session_token("sess", "prov")

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            return _async_source(_canned_text_turn())

        monkeypatch.setattr("app.core.translator_gateway.router._load_provider", _fake_provider)
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        big = "hello there this is a long message " * 50
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "gpt-5-codex",
                      "messages": [{"role": "user", "content": big}], "stream": False},
            )
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "Hello world"


# ─── Chunk F: leg dispatch through the endpoint (stubbed provider + upstream) ─

class TestLegDispatch:
    """The subagent model dispatches to the subagent provider; every other model
    (and count_tokens for it) dispatches to the main provider. Uses a stubbed
    provider fetch + stubbed upstream so the finite buffered body is safe through
    ASGITransport."""

    def setup_method(self):
        routing.get_registry().clear()

    def _stub(self, monkeypatch):
        loaded: List[str] = []
        streamed: List[str] = []

        async def _fake_provider(provider_id):
            loaded.append(provider_id)
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            streamed.append(provider_id)
            return _async_source(_canned_text_turn())

        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)
        return loaded, streamed

    async def test_subagent_model_dispatches_to_subagent_provider(self, monkeypatch):
        loaded, streamed = self._stub(monkeypatch)
        token = routing.mint_session_token(
            "sess", "main-prov",
            legs=(("gpt-5-codex", "sub-prov", None),),
        )
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5-codex",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 200
        assert loaded == ["sub-prov"]     # loaded the SUBAGENT leg's provider row
        assert streamed == ["sub-prov"]   # dispatched to the SUBAGENT provider

    async def test_other_model_dispatches_to_main_provider(self, monkeypatch):
        loaded, streamed = self._stub(monkeypatch)
        token = routing.mint_session_token(
            "sess", "main-prov",
            legs=(("gpt-5-codex", "sub-prov", None),),
        )
        async with await _client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "claude-sonnet-4",  # not the subagent model → MAIN
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 200
        assert loaded == ["main-prov"]
        assert streamed == ["main-prov"]

    async def test_count_tokens_honors_subagent_leg(self, monkeypatch):
        loaded, _ = self._stub(monkeypatch)
        token = routing.mint_session_token(
            "sess", "main-prov",
            legs=(("gpt-5-codex", "sub-prov", None),),
        )
        async with await _client() as c:
            sub = await c.post(
                "/v1/messages/count_tokens",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "gpt-5-codex",
                      "messages": [{"role": "user", "content": "hello there"}]},
            )
            main = await c.post(
                "/v1/messages/count_tokens",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "claude-sonnet-4",
                      "messages": [{"role": "user", "content": "hello there"}]},
            )
        assert sub.status_code == 200 and main.status_code == 200
        assert sub.json()["input_tokens"] > 0
        # count_tokens for the subagent model loaded the subagent provider, and
        # for the main model loaded the main provider.
        assert loaded == ["sub-prov", "main-prov"]

    async def test_anthropic_main_and_codex_leg_dispatch(self, monkeypatch):
        """Flagship mixed binding: an anthropic-type MAIN + a Codex subagent leg.
        A request for the MAIN model goes down anthropic_passthrough against the
        MAIN provider row; a request for the SUBAGENT model goes down the codex
        (translate) path against the LEG provider row."""
        passthrough_calls: List[str] = []
        codex_calls: List[str] = []

        async def _fake_provider(provider_id):
            if provider_id == "sub-prov":
                return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}
            return {"id": provider_id, "provider_type": "anthropic"}

        async def _fake_passthrough_json(*, provider_id, anthropic_body, client=None):
            passthrough_calls.append(provider_id)
            return {
                "id": "msg_1", "type": "message", "role": "assistant",
                "model": anthropic_body.get("model"),
                "content": [{"type": "text", "text": "hi from anthropic"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }

        def _fake_stream(*, provider_id, mode, responses_body, client=None):
            assert mode == "codex"
            codex_calls.append(provider_id)
            return _async_source(_canned_text_turn())

        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )
        monkeypatch.setattr(upstream, "anthropic_passthrough_json", _fake_passthrough_json)
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        token = routing.mint_session_token(
            "sess", "main-prov",
            legs=(("gpt-5-codex", "sub-prov", None),),
        )
        async with await _client() as c:
            # MAIN model → anthropic_passthrough against the MAIN provider row.
            main_resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "claude-sonnet-4",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            # SUBAGENT model → codex (translate) path against the LEG provider row.
            sub_resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5-codex",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert main_resp.status_code == 200
        assert main_resp.json()["content"][0]["text"] == "hi from anthropic"
        assert sub_resp.status_code == 200
        assert sub_resp.json()["content"][0]["text"] == "Hello world"
        assert passthrough_calls == ["main-prov"]  # main leg → anthropic passthrough
        assert codex_calls == ["sub-prov"]         # subagent leg → codex translate


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
