import asyncio
import base64
import io
import json
import wave
from unittest.mock import AsyncMock
import pytest
from app.core import voice_preview

class Socket:
    def __init__(self, events): self.events = events; self.sent = []
    async def send(self, raw): self.sent.append(json.loads(raw))
    async def recv(self): return json.dumps({"type":"session.started"})
    def __aiter__(self): return self
    async def __anext__(self):
        if not self.events: raise StopAsyncIteration
        await asyncio.sleep(0)
        return json.dumps(self.events.pop(0))
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

@pytest.mark.asyncio
async def test_real_live_sample_protocol_wav_and_close(monkeypatch):
    frame = base64.b64encode(b"\xff\x0f" * 2400).decode()
    silence = base64.b64encode(bytes(48000)).decode()
    socket = Socket([{"type":"session.output_transcript.delta", "delta":"Hello"},
        {"type":"session.output_audio.delta", "delta":frame},
        {"type":"session.output_audio.delta", "delta":silence}])
    monkeypatch.setattr(voice_preview, "connect", lambda *a, **kw: socket)
    data = await voice_preview.generate_live_preview("secret", "quartz")
    assert socket.sent[0]["session"]["model"] == "gpt-live-1"
    assert socket.sent[0]["session"]["audio"]["output"]["voice"] == "quartz"
    assert socket.sent[-1] == {"type":"session.close"}
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000,1,2)
        assert wav.getnframes() == 7200
    assert voice_preview._active_previews == 0

@pytest.mark.asyncio
async def test_preview_cancellation_releases_upstream_and_capacity(monkeypatch):
    started=asyncio.Event()
    class WaitingSocket(Socket):
        async def __anext__(self):
            started.set()
            await asyncio.Future()
    socket=WaitingSocket([])
    monkeypatch.setattr(voice_preview, "connect", lambda *a, **kw: socket)
    task=asyncio.create_task(voice_preview.generate_live_preview("secret", "stone"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert socket.sent[-1] == {"type":"session.close"}
    assert voice_preview._active_previews == 0

@pytest.mark.asyncio
async def test_preview_capacity_and_errors_release_slots(monkeypatch):
    monkeypatch.setattr(voice_preview, "_active_previews", 2)
    with pytest.raises(Exception) as error:
        await voice_preview.generate_live_preview("secret", "stone")
    assert error.value.status_code == 429
    monkeypatch.setattr(voice_preview, "_active_previews", 0)
    monkeypatch.setattr(voice_preview, "_generate", AsyncMock(side_effect=ValueError("no voice")))
    with pytest.raises(ValueError): await voice_preview.generate_live_preview("secret", "stone")
    assert voice_preview._active_previews == 0

@pytest.mark.asyncio
async def test_grok_sample_uses_its_own_voice_protocol_and_key(monkeypatch):
    frame = base64.b64encode(b"\xff\x0f" * 2400).decode()
    class GrokSocket(Socket):
        async def recv(self): return json.dumps({"type": "session.updated"})
    socket = GrokSocket([{"type": "response.output_audio.delta", "delta": frame}, {"type": "response.done"}])
    connections = []
    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return socket
    monkeypatch.setattr(voice_preview, "connect", connect)
    data = await voice_preview.generate_live_preview("xai-test-key", "eve", "grok-voice-latest")
    assert connections[0][0] == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert connections[0][1]["additional_headers"] == {"Authorization": "Bearer xai-test-key"}
    assert socket.sent[0]["session"]["voice"] == "eve"
    assert socket.sent[0]["session"]["tools"] == []
    assert socket.sent[0]["session"]["turn_detection"] is None
    assert socket.sent[-1] == {"type": "response.create"}
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnframes()) == (24000, 2400)
    assert voice_preview._active_previews == 0
