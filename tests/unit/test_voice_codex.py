"""Unofficial subscription voice: fake HTTP/peer transports, never real credentials."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.voice_codex_transport import CodexPeer, codex_session, incoming_event, outgoing_event


def test_request_matches_probed_codex_shape():
    session = codex_session({"voice": "cove"}, {"instructions": "prompt"}, [])
    assert session == {"model": "gpt-live-1-codex", "instructions": "prompt",
                       "audio": {"output": {"voice": "cove"}}, "delegation": {"type": "client"}}
    assert "input" not in session and "type" not in session


def test_protocol_maps_transcripts_and_handoffs():
    assert incoming_event({"type": "session.started"}) == {"type": "session.started"}
    assert incoming_event({"type": "input_transcript.added", "item": {"text": "hello"}}) == {
        "type": "session.input_transcript.delta", "delta": "hello"}
    assert incoming_event({"type": "output_transcript.added", "item": {"text": "hi"}})["delta"] == "hi"
    assert incoming_event({"type": "delegation.created", "item": {
        "type": "delegation", "target": "client", "id": "d1"}}) == {
        "type": "session.delegation.created", "delegation": {"id": "d1"}}
    assert incoming_event({"type": "delegation.created", "item": {"target": "server"}}) is None


def test_errors_never_relay_upstream_text():
    secret = "synthetic-private-value"
    event = incoming_event({"type": "error", "error": {"message": secret, "token": secret}})
    assert secret not in json.dumps(event)
    assert event["type"] == "error"


def test_context_updates_use_codex_content_arrays():
    assert outgoing_event({"type": "session.commentary.append", "content": "done", "delegation_id": "d1"}) == {
        "type": "delegation.context.append", "delegation_item_id": "d1", "channel": "speakable",
        "content": [{"type": "input_text", "text": "done"}]}
    assert outgoing_event({"type": "session.thinking.append", "content": "working"})["channel"] == "commentary"


class FakePeer:
    def __init__(self):
        self.handlers = {}
        self.channel = self
        self.readyState = "open"
        self.localDescription = type("SDP", (), {"sdp": "synthetic-offer"})()
        self.close = AsyncMock()
        self.sent = []
    def on(self, kind):
        def register(fn):
            self.handlers[kind] = fn
            return fn
        return register
    def createDataChannel(self, name):
        assert name == "oai-events"
        return self
    async def createOffer(self):
        return self.localDescription
    async def setLocalDescription(self, offer):
        pass
    async def setRemoteDescription(self, answer):
        self.handlers["message"](json.dumps({"type": "session.started"}))
    def send(self, data):
        self.sent.append(json.loads(data))


@pytest.mark.asyncio
async def test_one_post_opens_session_and_closes_peer():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.host == "chatgpt.com"
        assert json.loads(request.content)["session"]["model"] == "gpt-live-1-codex"
        return httpx.Response(201, text="synthetic-answer")
    fake = FakePeer()
    peer = CodexPeer()
    peer.make_peer = lambda: (fake, lambda audio: None, lambda sdp: sdp)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await peer.open(client, "fake-token", "fake-account", {"model": "gpt-live-1-codex"})
    assert json.loads(await peer.recv())["type"] == "session.started"
    assert len(seen) == 1
    await peer.close()
    fake.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_refusal_closes_peer_without_body_or_headers_in_error():
    fake = FakePeer()
    peer = CodexPeer()
    peer.make_peer = lambda: (fake, lambda audio: None, lambda sdp: sdp)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        403, json={"error": {"message": "synthetic-private-value"}}))) as client:
        with pytest.raises(ValueError, match="HTTP 403") as error:
            await peer.open(client, "fake-token", None, {})
    assert "synthetic-private-value" not in str(error.value)
    fake.close.assert_awaited_once()


def test_codex_is_explicit_and_never_funds_api_key_models():
    from app.api.voice import _is_eligible_voice_provider, provider_matches_model
    row = {"id": "sub", "provider_type": "chatgpt", "enabled": True,
           "codex_access_token": "fake-encrypted-bundle"}
    assert _is_eligible_voice_provider(row)
    assert provider_matches_model(row, "gpt-live-1-codex")
    for model in ("gpt-live-1", "grok-voice-latest", "gpt-realtime"):
        assert not provider_matches_model(row, model)
    assert not _is_eligible_voice_provider({**row, "needs_reauth": True})
    assert not _is_eligible_voice_provider({**row, "enabled": False})
    assert not provider_matches_model({"provider_type": "openai"}, "gpt-live-1-codex")


@pytest.mark.asyncio
async def test_subclass_waits_for_acceptance_with_same_chat_history_and_no_api_socket(monkeypatch):
    from app.core.voice_codex import CodexVoiceStream
    from app.core import http_client
    from app.core.provider import fluid_credentials
    peer = AsyncMock()
    peer.enqueue = Mock()
    peer.recv.side_effect = [json.dumps({"type": "session.updated"}), json.dumps({"type": "session.started"})]
    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", AsyncMock(return_value=("fake-token", "fake-account")))
    monkeypatch.setattr(http_client, "get_http_client", lambda: "fake-http")
    call = CodexVoiceStream(AsyncMock(), {"id": "chat"}, {
        "model": "gpt-live-1-codex", "voice": "cove", "provider_id": "sub"}, "fake-token", 5)
    call.upstream = peer
    await call.start_live([{"role":"user", "content":[{"type":"input_text", "text":"earlier"}]}])
    session = peer.open.call_args.args[-1]
    assert session["initial_items"][0]["content"][0]["text"] == "earlier"
    assert peer.open.call_args.args[:3] == ("fake-http", "fake-token", "fake-account")
    assert call.is_live
    assert call.reconnect_reason(None, None) is None
    assert call.api_key == ""


@pytest.mark.asyncio
async def test_subclass_sanitizes_provider_exceptions(monkeypatch):
    from app.core.voice_codex import CodexVoiceStream
    from app.core import http_client
    from app.core.provider import fluid_credentials
    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", AsyncMock(return_value=("fake-token", None)))
    monkeypatch.setattr(http_client, "get_http_client", lambda: "fake-http")
    call = CodexVoiceStream(AsyncMock(), {"id": "chat"}, {
        "model":"gpt-live-1-codex", "voice":"cove", "provider_id":"sub"}, "fake-token", 5)
    call.upstream = AsyncMock()
    call.upstream.open.side_effect = RuntimeError("synthetic-private-value")
    with pytest.raises(ValueError) as error:
        await call.start_live([])
    assert "synthetic-private-value" not in str(error.value)
