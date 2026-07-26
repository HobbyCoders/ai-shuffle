"""Codex ``session_id`` header stability — prompt-cache affinity.

The Codex backend keys prompt-cache affinity on the ``session_id`` request
header (docs/CODEX_SPIKE_FINDINGS.md). It used to be a fresh ``uuid4()`` on
every request, which made every turn cache-cold and reprocessed the whole
context at full cost. It is now derived from the gateway
:class:`~app.core.translator_gateway.routing.RouteBinding` session id, so:

  * two requests for the SAME binding carry the SAME ``session_id`` header;
  * two requests for DIFFERENT bindings carry DIFFERENT ones;
  * a request with no session bound falls back to a fresh per-request uuid.

The header is observed for real: ``stream_responses_events`` is driven against
an ``httpx.MockTransport`` that records the outgoing request headers, and the
router-level test goes through the actual endpoint via ASGITransport with a
finite (``[DONE]``-only) body.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import httpx
import pytest

from app.core import encryption
from app.core.translator_gateway import routing, upstream
from app.core.translator_gateway.service import build_gateway_app
from app.db import database as db


# ─── helpers ─────────────────────────────────────────────────────────────────

def _seed_codex_provider(provider_id: str, account_id: str = "acct-1") -> None:
    db.create_model_provider(
        provider_id=provider_id,
        name="Codex",
        provider_type="chatgpt",
        base_url="https://chatgpt.com/backend-api/codex",
        enabled=True,
    )
    db.set_codex_token_bundle(
        provider_id,
        access_token=encryption.encrypt_value("access-tok"),
        refresh_token=encryption.encrypt_value("refresh-tok"),
        account_id=account_id,
        id_token_expires_at=None,
        last_refresh=None,
        needs_reauth=False,
    )


def _recording_client(seen: List[Dict[str, str]]) -> httpx.AsyncClient:
    """A mock-transport client that records every outgoing request's headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _drain(provider_id: str, session_id, client: httpx.AsyncClient) -> None:
    async for _ in upstream.stream_responses_events(
        provider_id=provider_id,
        mode="codex",
        responses_body={"model": "gpt-5-codex"},
        session_id=session_id,
        client=client,
    ):
        pass


# ─── pure derivation ─────────────────────────────────────────────────────────

class TestCodexSessionUuid:
    def test_same_session_id_is_stable(self):
        first = upstream.codex_session_uuid("sess-a")
        second = upstream.codex_session_uuid("sess-a")
        assert first == second

    def test_different_session_ids_differ(self):
        assert upstream.codex_session_uuid("sess-a") != upstream.codex_session_uuid("sess-b")

    def test_result_is_a_valid_uuid_string(self):
        # The upstream contract documents this header as a UUID; an arbitrary
        # gateway session id must still render as one.
        value = upstream.codex_session_uuid("not-a-uuid-at-all")
        assert str(uuid.UUID(value)) == value

    def test_session_id_is_not_sent_verbatim(self):
        assert upstream.codex_session_uuid("sess-a") != "sess-a"

    @pytest.mark.parametrize("missing", [None, ""])
    def test_missing_session_falls_back_to_fresh_uuid(self, missing):
        first = upstream.codex_session_uuid(missing)
        second = upstream.codex_session_uuid(missing)
        assert first != second
        assert str(uuid.UUID(first)) == first


# ─── header construction ─────────────────────────────────────────────────────

class TestCodexHeaders:
    def test_headers_carry_the_derived_session_id(self):
        headers = upstream._codex_headers("tok", "acct", "sess-a")
        assert headers["session_id"] == upstream.codex_session_uuid("sess-a")

    def test_headers_without_session_are_per_request_unique(self):
        a = upstream._codex_headers("tok", "acct")
        b = upstream._codex_headers("tok", "acct")
        assert a["session_id"] != b["session_id"]


# ─── real requests through stream_responses_events ───────────────────────────

class TestStreamSendsStableSessionId:
    async def test_two_requests_same_session_share_the_header(self, mock_db):
        encryption.set_encryption_key("test-password", db)
        _seed_codex_provider("prov-sess-1")

        seen: List[Dict[str, str]] = []
        async with _recording_client(seen) as client:
            await _drain("prov-sess-1", "sess-a", client)
            await _drain("prov-sess-1", "sess-a", client)

        assert len(seen) == 2
        assert seen[0]["session_id"] == seen[1]["session_id"]

    async def test_two_different_sessions_send_different_headers(self, mock_db):
        encryption.set_encryption_key("test-password", db)
        _seed_codex_provider("prov-sess-2")

        seen: List[Dict[str, str]] = []
        async with _recording_client(seen) as client:
            await _drain("prov-sess-2", "sess-a", client)
            await _drain("prov-sess-2", "sess-b", client)

        assert len(seen) == 2
        assert seen[0]["session_id"] != seen[1]["session_id"]

    async def test_no_session_still_sends_a_fresh_uuid(self, mock_db):
        encryption.set_encryption_key("test-password", db)
        _seed_codex_provider("prov-sess-3")

        seen: List[Dict[str, str]] = []
        async with _recording_client(seen) as client:
            await _drain("prov-sess-3", None, client)
            await _drain("prov-sess-3", None, client)

        assert len(seen) == 2
        assert seen[0]["session_id"] != seen[1]["session_id"]
        assert str(uuid.UUID(seen[0]["session_id"])) == seen[0]["session_id"]


# ─── end-to-end through the gateway endpoint ─────────────────────────────────

class TestRouterThreadsBindingSessionId:
    """The binding's session id reaches the upstream leg from the real endpoint."""

    def setup_method(self):
        routing.get_registry().clear()

    async def _post(self, client: httpx.AsyncClient, token: str):
        return await client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "gpt-5-codex",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    async def _run(self, monkeypatch, sessions: List[str]) -> List[Any]:
        """POST once per entry in ``sessions``; return the session_id values the
        upstream leg was called with."""
        captured: List[Any] = []

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        def _fake_stream(*, provider_id, mode, responses_body, session_id=None, client=None):
            captured.append(session_id)

            async def _empty():
                if False:  # pragma: no cover — an empty async generator
                    yield None

            return _empty()

        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )
        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)

        transport = httpx.ASGITransport(app=build_gateway_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            for session in sessions:
                token = routing.mint_session_token(session, "prov-main")
                resp = await self._post(c, token)
                assert resp.status_code == 200
        return captured

    async def test_same_binding_reused_across_requests(self, monkeypatch):
        captured = await self._run(monkeypatch, ["sess-a", "sess-a"])
        assert captured == ["sess-a", "sess-a"]
        assert (
            upstream.codex_session_uuid(captured[0])
            == upstream.codex_session_uuid(captured[1])
        )

    async def test_different_bindings_get_different_session_ids(self, monkeypatch):
        captured = await self._run(monkeypatch, ["sess-a", "sess-b"])
        assert captured == ["sess-a", "sess-b"]
        assert (
            upstream.codex_session_uuid(captured[0])
            != upstream.codex_session_uuid(captured[1])
        )
