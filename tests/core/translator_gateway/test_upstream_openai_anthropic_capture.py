"""Tests for the OpenAI/Anthropic usage-header capture hooks wired into
``upstream.py`` (#757, the deferred rows 3-4 of #696's tier table).

Mirrors ``test_upstream_codex_capture.py``: drives the real upstream
functions against an ``httpx.MockTransport`` so the capture points fire for
real, and verifies capture failures never interrupt the actual response
path. No live provider calls anywhere in this module.
"""

import httpx

from app.core.translator_gateway import upstream
from app.db import database as db


def _seed_openai_provider(provider_id: str = "prov-openai-cap") -> str:
    db.create_model_provider(
        provider_id=provider_id,
        name="OpenAI",
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        enabled=True,
        auth_token="sk-test-key",
    )
    return provider_id


def _seed_anthropic_provider(provider_id: str = "prov-anthropic-cap") -> str:
    db.create_model_provider(
        provider_id=provider_id,
        name="Anthropic",
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        enabled=True,
        auth_token="sk-ant-test-key",
    )
    return provider_id


def _sse_body() -> bytes:
    return b"data: [DONE]\n\n"


class TestOpenAiCaptureFires:
    async def test_openai_mode_invokes_capture_with_headers(self, mock_db, monkeypatch):
        provider_id = _seed_openai_provider()
        response_headers = {
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-remaining-requests": "59",
        }

        def handler(request):
            return httpx.Response(200, headers=response_headers, content=_sse_body())

        calls = []

        def _fake_capture(pid, headers):
            calls.append((pid, dict(headers)))

        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(provider_usage_module, "capture_openai_headers", _fake_capture)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=provider_id,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
            ]

        assert events == []  # [DONE]-only body yields no events
        assert len(calls) == 1
        called_provider_id, called_headers = calls[0]
        assert called_provider_id == provider_id
        assert called_headers.get("x-ratelimit-limit-requests") == "60"

    async def test_codex_mode_never_invokes_openai_capture(self, mock_db, monkeypatch):
        from app.core import encryption

        encryption.set_encryption_key("test-password", db)
        provider_id = "prov-codex-no-openai-cap"
        db.create_model_provider(
            provider_id=provider_id,
            name="Codex",
            provider_type="custom",
            base_url="https://chatgpt.com/backend-api/codex",
            enabled=True,
        )
        db.set_codex_token_bundle(
            provider_id,
            access_token=encryption.encrypt_value("access-tok"),
            refresh_token=encryption.encrypt_value("refresh-tok"),
            account_id="acct-1",
            id_token_expires_at=None,
            last_refresh=None,
            needs_reauth=False,
        )

        def handler(request):
            return httpx.Response(
                200,
                headers={"x-ratelimit-limit-requests": "60"},  # ignored in codex mode
                content=_sse_body(),
            )

        calls = []
        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(
            provider_usage_module, "capture_openai_headers", lambda *a, **k: calls.append(1)
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=provider_id,
                    mode="codex",
                    responses_body={"model": "gpt-5-codex"},
                    client=client,
                )
            ]

        assert events == []
        assert calls == []

    async def test_capture_exception_does_not_interrupt_openai_stream(self, mock_db, monkeypatch):
        provider_id = _seed_openai_provider("prov-openai-cap-boom")

        def handler(request):
            body = (
                b"event: response.output_text.delta\n"
                b'data: {"type": "response.output_text.delta", "delta": "hi"}\n\n'
            )
            return httpx.Response(
                200, headers={"x-ratelimit-limit-requests": "60"}, content=body
            )

        from app.core import provider_usage as provider_usage_module

        def _boom(*a, **k):
            raise RuntimeError("capture blew up")

        monkeypatch.setattr(provider_usage_module, "capture_openai_headers", _boom)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=provider_id,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
            ]

        assert len(events) == 1
        assert events[0][0] == "response.output_text.delta"


class TestAnthropicPassthroughCaptureFires:
    async def test_json_passthrough_invokes_capture_with_headers(self, mock_db, monkeypatch):
        provider_id = _seed_anthropic_provider()
        response_headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "45",
        }

        def handler(request):
            return httpx.Response(
                200, headers=response_headers, json={"id": "msg_1", "content": []}
            )

        calls = []

        def _fake_capture(pid, headers):
            calls.append((pid, dict(headers)))

        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(provider_usage_module, "capture_anthropic_headers", _fake_capture)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await upstream.anthropic_passthrough_json(
                provider_id=provider_id,
                anthropic_body={"model": "claude-x", "messages": []},
                client=client,
            )

        assert body == {"id": "msg_1", "content": []}
        assert len(calls) == 1
        called_provider_id, called_headers = calls[0]
        assert called_provider_id == provider_id
        assert called_headers.get("anthropic-ratelimit-requests-limit") == "50"

    async def test_stream_passthrough_invokes_capture_with_headers(self, mock_db, monkeypatch):
        provider_id = _seed_anthropic_provider("prov-anthropic-cap-stream")
        response_headers = {
            "anthropic-ratelimit-tokens-limit": "40000",
            "anthropic-ratelimit-tokens-remaining": "39000",
        }

        def handler(request):
            # `stream=` (not `content=`) so `resp.aiter_raw()` (what
            # `anthropic_passthrough_stream` actually calls) sees a real,
            # not-yet-consumed stream — MockTransport + `content=<bytes>`
            # marks `is_stream_consumed` True before any read, an httpx quirk
            # unrelated to the capture hook under test.
            return httpx.Response(
                200, headers=response_headers, stream=httpx.ByteStream(b"event: ping\n\n")
            )

        calls = []

        def _fake_capture(pid, headers):
            calls.append((pid, dict(headers)))

        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(provider_usage_module, "capture_anthropic_headers", _fake_capture)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            chunks = [
                c
                async for c in upstream.anthropic_passthrough_stream(
                    provider_id=provider_id,
                    anthropic_body={"model": "claude-x", "messages": []},
                    client=client,
                )
            ]

        assert b"".join(chunks) == b"event: ping\n\n"
        assert len(calls) == 1
        called_provider_id, called_headers = calls[0]
        assert called_provider_id == provider_id
        assert called_headers.get("anthropic-ratelimit-tokens-limit") == "40000"

    async def test_capture_exception_does_not_interrupt_json_passthrough(
        self, mock_db, monkeypatch
    ):
        provider_id = _seed_anthropic_provider("prov-anthropic-cap-boom")

        def handler(request):
            return httpx.Response(
                200,
                headers={"anthropic-ratelimit-requests-limit": "50"},
                json={"id": "msg_2", "content": []},
            )

        from app.core import provider_usage as provider_usage_module

        def _boom(*a, **k):
            raise RuntimeError("capture blew up")

        monkeypatch.setattr(provider_usage_module, "capture_anthropic_headers", _boom)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await upstream.anthropic_passthrough_json(
                provider_id=provider_id,
                anthropic_body={"model": "claude-x", "messages": []},
                client=client,
            )

        assert body == {"id": "msg_2", "content": []}

    async def test_429_never_invokes_capture(self, mock_db, monkeypatch):
        provider_id = _seed_anthropic_provider("prov-anthropic-cap-429")

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "5"}, content=b"limit")

        calls = []
        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(
            provider_usage_module,
            "capture_anthropic_headers",
            lambda *a, **k: calls.append(1),
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                await upstream.anthropic_passthrough_json(
                    provider_id=provider_id,
                    anthropic_body={"model": "claude-x", "messages": []},
                    client=client,
                )
            except upstream.UpstreamError:
                pass

        assert calls == []
