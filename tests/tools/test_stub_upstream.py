"""Verification for the local stub upstream (``tools/stub_upstream``).

The bar set for this harness is that it be proven by driving the GATEWAY'S OWN
client code against it — not by asserting the stub returns bytes. So every test
below calls the real
``app.core.translator_gateway.upstream.stream_responses_events`` (or the real
passthrough helpers) over a real TCP socket to a real stub process-thread.

Two interception routes are covered, because the harness offers both:

  1. ``StubRedirectTransport`` — an httpx transport handed to the gateway's
     ``client=`` seam. Used by tests.
  2. The MITM proxy + ``HTTPS_PROXY`` / ``SSL_CERT_FILE`` env — the route a
     RUNNING app instance must use, since ``upstream.py`` hardcodes its vendor
     URLs. Verified here against the shared ``get_http_client()`` so the
     production code path (no ``client=`` argument) is what gets exercised.

NO NETWORK EGRESS: every socket in this file terminates on 127.0.0.1. The proxy
never dials out — an unrecognized host still gets a canned stub response.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from app.core import encryption
from app.core.translator_gateway import upstream
from app.core.translator_gateway.responses_to_anthropic import (
    ResponsesToAnthropicTranslator,
)
from app.db import database as db
from tools.stub_upstream import scenarios as S
from tools.stub_upstream.redirect import StubRedirectTransport
from tools.stub_upstream.server import StubState, serve

OPENAI_PROVIDER = "prov-stub-openai"
CODEX_PROVIDER = "prov-stub-codex"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stub():
    """A running stub on a random loopback port. Yields ``(state, port)``."""
    state = StubState(max_hang_seconds=5.0)
    port = _free_port()
    httpd, _thread, _ = serve(port=port, state=state)
    try:
        yield state, port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _client(port: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=StubRedirectTransport(port))


def _seed_openai_provider():
    db.create_model_provider(
        provider_id=OPENAI_PROVIDER,
        name="Stub OpenAI",
        provider_type="openai",
        base_url="http://127.0.0.1:8799",
        enabled=True,
        auth_token="sk-stub-not-a-real-key",
    )


def _seed_codex_provider(mock_db):
    encryption.set_encryption_key("test-password", db)
    db.create_model_provider(
        provider_id=CODEX_PROVIDER,
        name="Stub Codex",
        provider_type="chatgpt",
        base_url="http://127.0.0.1:8799",
        enabled=True,
    )
    db.set_codex_token_bundle(
        CODEX_PROVIDER,
        access_token=encryption.encrypt_value("stub-access-token"),
        refresh_token=encryption.encrypt_value("stub-refresh-token"),
        account_id="stub-account-id",
        id_token_expires_at=None,
        last_refresh=None,
        needs_reauth=False,
    )


async def _drain(port: int, provider_id: str, mode: str, body=None):
    async with _client(port) as client:
        return [
            ev
            async for ev in upstream.stream_responses_events(
                provider_id=provider_id,
                mode=mode,
                responses_body=body or {"model": "gpt-5.6-sol", "stream": True},
                client=client,
            )
        ]


# ═══ 1. A normal streaming reply parses through the real gateway client ══════

class TestNormalStreamingReply:
    async def test_openai_mode_events_reach_the_real_parser(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(S.scenario_from_dict({"name": "text", "text": "HELLO STUB"}))

        events = await _drain(port, OPENAI_PROVIDER, "openai")

        names = [n for n, _ in events]
        assert names[0] == "response.created"
        assert names[-1] == "response.completed"
        assert "response.output_text.delta" in names
        # Fidelity: the real backend does NOT emit a [DONE] sentinel, so the
        # parser must see the stream simply end after response.completed.
        assert all(n != "[DONE]" for n in names)

    async def test_translator_reassembles_the_assistant_text(self, mock_db, stub):
        """End-to-end: stub bytes -> upstream.iter_sse_events -> the real
        Responses->Anthropic translator -> assembled final turn."""
        state, port = stub
        _seed_openai_provider()
        state.set_default(
            S.scenario_from_dict({"name": "text", "text": "HELLO STUB", "n_deltas": 5})
        )

        translator = ResponsesToAnthropicTranslator(request_model="gpt-5.6-sol")
        async with _client(port) as client:
            async for name, payload in upstream.stream_responses_events(
                provider_id=OPENAI_PROVIDER,
                mode="openai",
                responses_body={"model": "gpt-5.6-sol", "stream": True},
                client=client,
            ):
                translator.push(name, payload)

        final = translator.final_response()
        texts = [getattr(i, "text", None) for i in final.items]
        assert "HELLO STUB" in texts
        assert final.usage.output_tokens > 0

    async def test_codex_mode_works_and_usage_headers_are_capturable(
        self, mock_db, stub, monkeypatch
    ):
        state, port = stub
        _seed_codex_provider(mock_db)
        state.set_default(
            S.scenario_from_dict(
                {
                    "name": "text",
                    "text": "CODEX OK",
                    "headers": {
                        "x-codex-plan-type": "pro",
                        "x-codex-primary-used-percent": "12",
                    },
                }
            )
        )
        captured = []
        from app.core import provider_usage

        monkeypatch.setattr(
            provider_usage,
            "capture_codex_headers",
            lambda pid, headers: captured.append((pid, dict(headers))),
        )

        events = await _drain(port, CODEX_PROVIDER, "codex")

        assert [n for n, _ in events][-1] == "response.completed"
        assert captured and captured[0][0] == CODEX_PROVIDER
        assert captured[0][1].get("x-codex-plan-type") == "pro"


# ═══ 2. Tool-call scenario (subagent-card progress frames) ═══════════════════

class TestToolCallScenario:
    async def test_tool_call_reassembles_byte_exact_arguments(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        args = '{"prompt":"do the thing","subagent_type":"general-purpose"}'
        state.set_default(
            S.scenario_from_dict(
                {
                    "name": "tool_call",
                    "tool_name": "Task",
                    "tool_arguments": args,
                    "tool_call_id": "call_stub_task",
                }
            )
        )

        translator = ResponsesToAnthropicTranslator(request_model="gpt-5.6-sol")
        frames = []
        async with _client(port) as client:
            async for name, payload in upstream.stream_responses_events(
                provider_id=OPENAI_PROVIDER,
                mode="openai",
                responses_body={"model": "gpt-5.6-sol", "stream": True},
                client=client,
            ):
                frames.extend(translator.push(name, payload))

        final = translator.final_response()
        tool_items = [i for i in final.items if getattr(i, "name", None) == "Task"]
        assert tool_items, "expected a tool_use item in the assembled turn"
        assert tool_items[0].call_id == "call_stub_task"
        assert json.loads(tool_items[0].arguments) == json.loads(args)
        # The streaming path must also have emitted incremental Anthropic frames.
        assert any(f.get("type") == "content_block_delta" for f in frames)


# ═══ 3. 429 → the same UpstreamError path a real rate limit takes ════════════

class TestRateLimitScenario:
    async def test_429_raises_upstream_error_with_status_and_body(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(
            S.scenario_from_dict({"name": "rate_limit", "retry_after": "37"})
        )

        with pytest.raises(upstream.UpstreamError) as exc:
            await _drain(port, OPENAI_PROVIDER, "openai")

        assert exc.value.status_code == 429
        assert "rate limit" in exc.value.message.lower()
        # Credential-free: _summarize_error_body only ever carries the body.
        assert "Bearer" not in exc.value.message

    async def test_retry_after_header_is_on_the_wire(self, mock_db, stub):
        """PR #761's breaker keys off Retry-After, so prove the header is real.

        Read straight off the HTTP response rather than through
        ``stream_responses_events``, which (in the base this branch was cut
        from) discards response headers on the >=400 path.
        """
        state, port = stub
        state.set_default(
            S.scenario_from_dict({"name": "rate_limit", "retry_after": "37"})
        )
        async with _client(port) as client:
            resp = await client.post(
                upstream.OPENAI_RESPONSES_URL, json={"model": "gpt-5.6-sol"}
            )
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "37"
        assert resp.json()["error"]["type"] == "rate_limit_exceeded"

    async def test_breaker_style_counting_queue(self, mock_db, stub):
        """A 429-429-429-then-OK queue: what a breaker test drives, and the
        request log is what counts the attempts."""
        state, port = stub
        _seed_openai_provider()
        state.set_default(S.scenario_from_dict({"name": "text", "text": "RECOVERED"}))
        state.set_queue(
            [S.scenario_from_dict({"name": "rate_limit", "retry_after": "1"})] * 3
        )

        for _ in range(3):
            with pytest.raises(upstream.UpstreamError) as exc:
                await _drain(port, OPENAI_PROVIDER, "openai")
            assert exc.value.status_code == 429

        events = await _drain(port, OPENAI_PROVIDER, "openai")
        assert [n for n, _ in events][-1] == "response.completed"

        assert state.request_count("/v1/responses") == 4
        statuses = [e["response_status"] for e in state.log_entries()]
        assert statuses == [429, 429, 429, 200]


# ═══ 4. auth error (codex refresh-once-then-retry) ══════════════════════════

class TestAuthErrorScenario:
    async def test_openai_401_surfaces_as_upstream_error(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(S.scenario_from_dict({"name": "auth_error"}))

        with pytest.raises(upstream.UpstreamError) as exc:
            await _drain(port, OPENAI_PROVIDER, "openai")
        assert exc.value.status_code == 401

    async def test_codex_401_triggers_exactly_one_refresh_attempt(
        self, mock_db, stub, monkeypatch
    ):
        """The stub makes the codex refresh-once branch reachable without a real
        account: 401, then whatever the refresh hook decides."""
        state, port = stub
        _seed_codex_provider(mock_db)
        state.set_default(S.scenario_from_dict({"name": "auth_error"}))

        calls = []
        from app.core import codex_token_refresh

        async def _fake_refresh(provider_id, *, stale_access_token=None, client=None):
            calls.append(provider_id)
            return False  # refresh failed -> UpstreamAuthError

        monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", _fake_refresh)

        with pytest.raises(upstream.UpstreamAuthError):
            await _drain(port, CODEX_PROVIDER, "codex")
        assert calls == [CODEX_PROVIDER]


# ═══ 5. slow + truncated + mid-stream error ═════════════════════════════════

class TestFailureShapes:
    async def test_slow_reply_still_parses(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(
            S.scenario_from_dict(
                {
                    "name": "slow",
                    "text": "SLOW",
                    "n_deltas": 2,
                    "delay_before_first_event": 0.05,
                    "delay_between_events": 0.01,
                }
            )
        )
        events = await _drain(port, OPENAI_PROVIDER, "openai")
        assert [n for n, _ in events][-1] == "response.completed"

    async def test_stream_error_ends_without_response_completed(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(
            S.scenario_from_dict({"name": "stream_error", "fail_after_events": 5})
        )
        events = await _drain(port, OPENAI_PROVIDER, "openai")
        names = [n for n, _ in events]
        assert "response.completed" not in names
        assert names[-2:] == ["response.failed", "error"]

    async def test_truncate_aborts_the_stream(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(
            S.scenario_from_dict({"name": "truncate", "fail_after_events": 4})
        )
        with pytest.raises(httpx.RemoteProtocolError):
            await _drain(port, OPENAI_PROVIDER, "openai")


# ═══ 6. request log ═════════════════════════════════════════════════════════

class TestRequestLog:
    async def test_log_records_body_and_redacts_credentials(self, mock_db, stub):
        state, port = stub
        _seed_openai_provider()
        state.set_default(S.scenario_from_dict({"name": "text", "text": "X"}))

        body = {"model": "gpt-5.6-sol", "stream": True, "store": False}
        await _drain(port, OPENAI_PROVIDER, "openai", body=body)

        entries = state.log_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["method"] == "POST"
        assert entry["path"] == "/v1/responses"
        assert entry["body"] == body
        # Host header still names the vendor the gateway believed it called.
        assert entry["headers"].get("Host", "").startswith("api.openai.com")
        auth = entry["headers"]["Authorization"]
        assert auth.startswith("<redacted ")
        assert "sk-stub-not-a-real-key" not in json.dumps(entry)

    async def test_codex_account_id_header_is_redacted(self, mock_db, stub):
        state, port = stub
        _seed_codex_provider(mock_db)
        state.set_default(S.scenario_from_dict({"name": "text", "text": "X"}))
        await _drain(port, CODEX_PROVIDER, "codex")

        blob = json.dumps(state.log_entries())
        assert "stub-access-token" not in blob
        assert "stub-account-id" not in blob
        assert "<redacted " in blob

    async def test_log_file_is_jsonl(self, mock_db, tmp_path):
        log_path = tmp_path / "stub.jsonl"
        state = StubState(log_path=str(log_path))
        port = _free_port()
        httpd, _t, _ = serve(port=port, state=state)
        try:
            _seed_openai_provider()
            await _drain(port, OPENAI_PROVIDER, "openai")
        finally:
            httpd.shutdown()
            httpd.server_close()
        lines = [json.loads(x) for x in log_path.read_text().splitlines() if x.strip()]
        assert len(lines) == 1 and lines[0]["path"] == "/v1/responses"


# ═══ 7. control plane + models catalog ══════════════════════════════════════

class TestControlPlane:
    async def test_scenario_queue_and_reset_over_http(self, stub):
        state, port = stub
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/__control__/scenario",
                json={"name": "text", "text": "VIA CONTROL"},
            )
            assert r.status_code == 200
            r = await c.post(
                f"{base}/__control__/queue",
                json={"queue": [{"name": "rate_limit", "repeat": 2}]},
            )
            assert r.json()["queue_depth"] == 2
            state_json = (await c.get(f"{base}/__control__/state")).json()
            assert state_json["default_scenario"]["text"] == "VIA CONTROL"
            assert state_json["queue_depth"] == 2

            assert (await c.post(f"{base}/v1/responses", json={})).status_code == 429
            assert (await c.post(f"{base}/v1/responses", json={})).status_code == 429
            ok = await c.post(f"{base}/v1/responses", json={})
            assert ok.status_code == 200
            assert b"VIA CONTROL" in ok.content

            assert (await c.get(f"{base}/__control__/count")).json()["count"] == 3
            await c.post(f"{base}/__control__/reset")
            assert (await c.get(f"{base}/__control__/count")).json()["count"] == 0

    async def test_per_request_header_override_wins(self, stub):
        _state, port = stub
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/v1/responses",
                json={},
                headers={"X-Stub-Scenario": "rate_limit"},
            )
        assert r.status_code == 429

    async def test_unknown_scenario_is_rejected(self, stub):
        _state, port = stub
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/__control__/scenario",
                json={"name": "definitely-not-a-scenario"},
            )
        assert r.status_code == 400

    async def test_models_catalog_matches_the_app_parser(self, stub):
        """``fetch_upstream_models`` is what a provider row's base_url hits."""
        from app.api.model_providers import fetch_upstream_models

        _state, port = stub
        async with httpx.AsyncClient() as client:
            models = await fetch_upstream_models(
                f"http://127.0.0.1:{port}", "dummy-token", client=client
            )
        assert models
        assert all(set(m) == {"id", "label"} for m in models)
        assert any(m["id"] == "gpt-5.6-sol" for m in models)


# ═══ 8. Anthropic passthrough (best-effort shape) ═══════════════════════════

class TestAnthropicPassthrough:
    async def test_passthrough_stream_yields_wellformed_anthropic_sse(
        self, mock_db, stub
    ):
        state, port = stub
        db.create_model_provider(
            provider_id="prov-stub-anthropic",
            name="Stub Anthropic",
            provider_type="anthropic",
            base_url="http://127.0.0.1:8799",
            enabled=True,
            auth_token="sk-ant-stub",
        )
        state.set_default(S.scenario_from_dict({"name": "text", "text": "ANTHROPIC"}))

        chunks = []
        async with _client(port) as client:
            async for chunk in upstream.anthropic_passthrough_stream(
                provider_id="prov-stub-anthropic",
                anthropic_body={"model": "claude-x", "stream": True},
                client=client,
            ):
                chunks.append(chunk)
        raw = b"".join(chunks).decode()
        assert "event: message_start" in raw
        assert "event: message_stop" in raw
        # The text arrives split across text_delta frames; reassemble it.
        text = "".join(
            json.loads(line[len("data:"):])["delta"]["text"]
            for line in raw.splitlines()
            if line.startswith("data:") and '"text_delta"' in line
        )
        assert text == "ANTHROPIC"

    async def test_passthrough_429_raises_upstream_error(self, mock_db, stub):
        state, port = stub
        db.create_model_provider(
            provider_id="prov-stub-anthropic-2",
            name="Stub Anthropic 2",
            provider_type="anthropic",
            base_url="http://127.0.0.1:8799",
            enabled=True,
            auth_token="sk-ant-stub",
        )
        state.set_default(S.scenario_from_dict({"name": "rate_limit"}))
        with pytest.raises(upstream.UpstreamError) as exc:
            async with _client(port) as client:
                async for _ in upstream.anthropic_passthrough_stream(
                    provider_id="prov-stub-anthropic-2",
                    anthropic_body={"model": "claude-x", "stream": True},
                    client=client,
                ):
                    pass
        assert exc.value.status_code == 429


# ═══ 9. The MITM proxy route — the PRODUCTION code path, no client= seam ════

class TestMitmProxyRoute:
    """Proves the running-app recipe: with HTTPS_PROXY + SSL_CERT_FILE set, the
    gateway's hardcoded ``https://api.openai.com`` request lands on the stub via
    the shared ``get_http_client()`` — no argument injection anywhere."""

    async def test_hardcoded_vendor_url_lands_on_the_stub(
        self, mock_db, tmp_path, monkeypatch
    ):
        from tools.stub_upstream import proxy as proxy_mod
        from app.core import http_client as http_client_mod

        state = StubState(max_hang_seconds=5.0)
        proxy_port = _free_port()
        httpd, _t, ca_pem = proxy_mod.serve_proxy(
            port=proxy_port, state=state, ca_dir=str(tmp_path / "ca")
        )
        try:
            _seed_openai_provider()
            state.set_default(
                S.scenario_from_dict({"name": "text", "text": "VIA PROXY"})
            )

            monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("SSL_CERT_FILE", ca_pem)
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
            monkeypatch.setattr(http_client_mod, "_client", None)

            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=OPENAI_PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5.6-sol", "stream": True},
                    # NOTE: no client= — this is the production path.
                )
            ]
            # NOTE: deliberately NOT http_client_mod.close_http_client() — that
            # helper calls AsyncClient.close(), which does not exist on httpx
            # 0.27 (it is aclose()). Pre-existing app bug, out of scope here.
            if http_client_mod._client is not None:
                await http_client_mod._client.aclose()
            http_client_mod._client = None
        finally:
            httpd.shutdown()
            httpd.server_close()

        assert [n for n, _ in events][-1] == "response.completed"
        entries = state.log_entries()
        assert len(entries) == 1
        assert entries[0]["via"] == "proxy"
        assert entries[0]["host"].startswith("api.openai.com")
        assert entries[0]["path"] == "/v1/responses"
        assert entries[0]["headers"]["Authorization"].startswith("<redacted ")
