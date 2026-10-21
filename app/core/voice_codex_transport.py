"""Unofficial Codex WebRTC transport; subscription credentials stay server-side.

Wire contract: openai/codex 430a16799d90e296fb27b1e94f206023902b7e56,
realtime_call.rs and methods_frameless_bidi.rs. Never log upstream bodies,
headers, SDP, bearer tokens or account identifiers. No automatic retry.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from fractions import Fraction
import json

CALL_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
CODEX_MODEL = "gpt-live-1-codex"


def codex_session(config: dict, options: dict, history: list[dict]) -> dict:
    from app.core.voice_prompts import LIVE_INSTRUCTIONS
    session = {"model": CODEX_MODEL, "instructions": options.get("instructions") or LIVE_INSTRUCTIONS,
               "audio": {"output": {"voice": config["voice"]}}, "delegation": {"type": "client"}}
    if history:
        session["initial_items"] = [
            {"type": "message", "role": item["role"], "content": [
                {"type": "output_text" if item["role"] == "assistant" else "input_text", "text": part["text"]}
                for part in item.get("content", []) if isinstance(part.get("text"), str)]}
            for item in history if item.get("role") in ("user", "assistant", "developer")]
    return session


def incoming_event(event: dict) -> dict | None:
    kind = event.get("type")
    if kind in ("error", "session.error"):
        return {"type": "error", "error": {"message": "Unofficial ChatGPT voice rejected the session or event."}}
    if kind in ("session.started", "session.updated", "session.closed"):
        return {"type": kind}
    if kind in ("input_transcript.added", "output_transcript.added"):
        return {"type": "session.input_transcript.delta" if kind.startswith("input") else "session.output_transcript.delta",
                "delta": (event.get("item") or {}).get("text") or ""}
    if kind == "delegation.created":
        item = event.get("item") or {}
        if item.get("type") == "delegation" and item.get("target") == "client" and item.get("id"):
            return {"type": "session.delegation.created", "delegation": {"id": item["id"]}}
    return None


def outgoing_event(event: dict) -> dict | None:
    kind = event.get("type")
    if kind in ("session.commentary.append", "session.thinking.append", "session.instructions.append"):
        did = event.get("delegation_id")
        result = {"type": "delegation.context.append" if did else "session.context.append",
                  "channel": "commentary" if kind == "session.thinking.append" else "speakable",
                  "content": [{"type": "input_text", "text": event["content"]}]}
        if did:
            result["delegation_item_id"] = did
        return result
    return None


class CodexPeer:
    """Bounded PCM/media + JSON event adapter for the existing VoiceStream."""
    def __init__(self):
        self.pc = None
        self.channel = None
        self.audio_tasks = set()
        self.events = asyncio.Queue(maxsize=128)
        self.closed = False
        self.failed = False
        self.feed_audio = None

    def make_peer(self):
        # Lazy import: the supported API-key transports do not depend on aiortc.
        from app.core.voice_dependencies import load_native
        native, media = load_native()
        AudioStreamTrack, RTCConfiguration = native.AudioStreamTrack, native.RTCConfiguration
        RTCPeerConnection, RTCSessionDescription = native.RTCPeerConnection, native.RTCSessionDescription
        AudioFrame = media.AudioFrame
        frames = deque(maxlen=50)  # At most one second of caller audio.
        pending = bytearray()

        class Microphone(AudioStreamTrack):
            samples = 0
            async def recv(self):
                await super().recv()  # Pace at 20 ms, including silence while muted.
                frame = AudioFrame(format="s16", layout="mono", samples=480)
                frame.sample_rate, frame.time_base = 24000, Fraction(1, 24000)
                frame.pts = self.samples
                self.samples += 480
                frame.planes[0].update(frames.popleft() if frames else bytes(960))
                return frame

        def feed(audio):
            pending.extend(audio)
            while len(pending) >= 960:
                frames.append(bytes(pending[:960]))
                del pending[:960]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        pc.addTrack(Microphone())
        return pc, feed, lambda sdp: RTCSessionDescription(sdp=sdp, type="answer")

    def enqueue(self, event):
        if self.closed or self.failed:
            return
        if self.events.full():
            # Never silently drop a handoff or error. End a stalled call instead.
            self.failed = True
            while not self.events.empty():
                self.events.get_nowait()
            event = {"type": "error", "error": {"message": "Unofficial ChatGPT voice event backlog exceeded."}}
        self.events.put_nowait(json.dumps(event))

    async def receive_audio(self, track):
        from av import AudioResampler
        from aiortc.mediastreams import MediaStreamError
        resampler = AudioResampler(format="s16", layout="mono", rate=24000)
        try:
            while not self.closed:
                for frame in resampler.resample(await track.recv()):
                    audio = bytes(frame.planes[0])[:frame.samples * 2]
                    self.enqueue({"type": "session.output_audio.delta", "delta": base64.b64encode(audio).decode()})
        except MediaStreamError:
            return
        except Exception:
            self.enqueue({"type": "error", "error": {"message": "Unofficial ChatGPT voice audio transport failed."}})

    async def open(self, client, token: str, account_id: str | None, session: dict):
        self.pc, self.feed_audio, answer = self.make_peer()
        self.channel = self.pc.createDataChannel("oai-events")

        @self.channel.on("message")
        def message(raw):
            try:
                payload = json.loads(raw)
                event = incoming_event(payload) if isinstance(payload, dict) else None
            except (ValueError, TypeError, AttributeError):
                event = {"type": "error", "error": {"message": "Invalid unofficial ChatGPT voice event."}}
            if event:
                self.enqueue(event)

        @self.pc.on("track")
        def track_received(track):
            if track.kind == "audio":
                task = asyncio.create_task(self.receive_audio(track))
                self.audio_tasks.add(task)
                task.add_done_callback(self.audio_tasks.discard)

        @self.pc.on("connectionstatechange")
        async def connection_changed():
            if self.pc.connectionState in ("failed", "closed", "disconnected") and not self.closed:
                self.enqueue({"type": "error", "error": {"message": "Unofficial ChatGPT voice connection ended."}})

        try:
            await self.pc.setLocalDescription(await self.pc.createOffer())
            headers = {"Authorization": "Bearer " + token, "originator": "codex_cli_rs", "OpenAI-Alpha": "quicksilver=v2"}
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
            response = await client.post(CALL_URL, headers=headers,
                                         json={"sdp": self.pc.localDescription.sdp, "session": session},
                                         follow_redirects=False, timeout=20)
            if response.status_code != 201:
                raise ValueError(f"Unofficial ChatGPT voice refused call creation (HTTP {response.status_code}).")
            await self.pc.setRemoteDescription(answer(response.text))
        except BaseException:
            await self.close()
            raise

    async def send(self, raw):
        event = json.loads(raw)
        if event.get("type") == "session.input_audio.append":
            self.feed_audio(base64.b64decode(event["audio"], validate=True))
        else:
            from app.core.voice_progress import split_bytes
            if self.channel.readyState != "open":
                raise ValueError("Unofficial ChatGPT voice data channel is not open.")
            # Codex caps each context append at 500 UTF-8 bytes.
            for chunk in split_bytes(event.get("content", ""), 500):
                wire = outgoing_event({**event, "content": chunk})
                if wire:
                    self.channel.send(json.dumps(wire))

    async def recv(self):
        return await self.events.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.recv()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in tuple(self.audio_tasks):
            task.cancel()
        await asyncio.gather(*self.audio_tasks, return_exceptions=True)
        if self.pc:
            await self.pc.close()
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(None)
