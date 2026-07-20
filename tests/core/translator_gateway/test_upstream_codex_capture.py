"""Tests for the Codex usage-header capture hook wired into
``upstream.py::stream_responses_events`` (Chunk U2).

Drives the real streaming function against an ``httpx.MockTransport`` so the
capture point (after the 400-guard, before the SSE loop) fires for real.
Verifies: capture is invoked ONLY for ``mode == "codex"`` (never ``openai``),
with the response headers passed through, and that a raised exception inside
the capture path does not interrupt SSE event iteration.
"""

import httpx
import pytest

from app.core import encryption
from app.core.translator_gateway import upstream
from app.db import database as db


PROVIDER_ID = "prov-codex-cap"


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _seed_codex_provider():
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Codex",
        provider_type="custom",
        base_url="https://chatgpt.com/backend-api/codex",
        enabled=True,
    )
    db.set_codex_token_bundle(
        PROVIDER_ID,
        access_token=encryption.encrypt_value("access-tok"),
        refresh_token=encryption.encrypt_value("refresh-tok"),
        account_id="acct-1",
        id_token_expires_at=None,
        last_refresh=None,
        needs_reauth=False,
    )


def _sse_body() -> bytes:
    return b"data: [DONE]\n\n"


class TestCaptureFiresOnCodexOnly:
    async def test_codex_mode_invokes_capture_with_headers(self, mock_db, monkeypatch):
        _unlock(mock_db)
        _seed_codex_provider()

        response_headers = {
            "x-codex-plan-type": "pro",
            "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "10080",
        }

        def handler(request):
            return httpx.Response(200, headers=response_headers, content=_sse_body())

        calls = []

        def _fake_capture(provider_id, headers):
            calls.append((provider_id, dict(headers)))

        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(provider_usage_module, "capture_codex_headers", _fake_capture)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=PROVIDER_ID,
                    mode="codex",
                    responses_body={"model": "gpt-5-codex"},
                    client=client,
                )
            ]

        assert events == []  # [DONE]-only body yields no events
        assert len(calls) == 1
        called_provider_id, called_headers = calls[0]
        assert called_provider_id == PROVIDER_ID
        assert called_headers.get("x-codex-plan-type") == "pro"

    async def test_openai_mode_never_invokes_capture(self, mock_db, monkeypatch):
        from app.db import database as db
        # Seed an OpenAI provider with a plaintext key.
        db.create_model_provider(
            provider_id="prov-openai-1",
            name="OpenAI",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            enabled=True,
            auth_token="sk-test-key",
        )

        response_headers = {"x-codex-plan-type": "pro"}  # should be ignored for openai mode

        def handler(request):
            return httpx.Response(200, headers=response_headers, content=_sse_body())

        calls = []

        from app.core import provider_usage as provider_usage_module

        monkeypatch.setattr(
            provider_usage_module, "capture_codex_headers", lambda *a, **k: calls.append(1)
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id="prov-openai-1",
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
            ]

        assert events == []
        assert calls == []


class TestCaptureFailureDoesNotBreakStream:
    async def test_capture_exception_does_not_interrupt_sse_iteration(
        self, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_codex_provider()

        def handler(request):
            body = (
                b"event: response.output_text.delta\n"
                b'data: {"type": "response.output_text.delta", "delta": "hi"}\n\n'
            )
            return httpx.Response(
                200, headers={"x-codex-plan-type": "pro"}, content=body
            )

        from app.core import provider_usage as provider_usage_module

        def _boom(*a, **k):
            raise RuntimeError("capture blew up")

        monkeypatch.setattr(provider_usage_module, "capture_codex_headers", _boom)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=PROVIDER_ID,
                    mode="codex",
                    responses_body={"model": "gpt-5-codex"},
                    client=client,
                )
            ]

        # The SSE event still arrives despite the capture path raising.
        assert len(events) == 1
        assert events[0][0] == "response.output_text.delta"
