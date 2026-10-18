"""Bounded, genuine GPT Live voice samples; no substitute TTS model or session row."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import wave

from websockets.asyncio.client import connect

from app.core.voice_models import LIVE_MODEL
from app.core.voice_stream import live_opening_event

# Only two funded preview connections at once per process. No background queue.
_active_previews = 0


async def generate_live_preview(api_key: str, voice: str, model: str = LIVE_MODEL) -> bytes:
    global _active_previews
    if _active_previews >= 2:
        from fastapi import HTTPException
        raise HTTPException(429, "Voice previews are busy. Try again in a moment.")
    _active_previews += 1
    try:
        async with asyncio.timeout(30):
            from app.core.voice_models import GROK_MODEL
            return await _generate_grok(api_key, voice) if model == GROK_MODEL else await _generate(api_key, voice)
    finally:
        _active_previews -= 1


async def _generate(api_key: str, voice: str) -> bytes:
    async with connect(
        "wss://api.openai.com/v1/live/sessions",
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=10, close_timeout=2, max_size=2_000_000,
    ) as ws:
        async def send(event):
            await ws.send(json.dumps(event))

        await send({"type": "session.start", "session": {
            "model": LIVE_MODEL,
            "instructions": "You are demonstrating your voice. Speak only the requested short greeting in English, then stay quiet. Do not delegate or take any actions.",
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": voice}},
            "delegation": {"type": "client"}, "input": [],
        }})
        event = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if event.get("type") != "session.started":
            raise ValueError("GPT Live could not start this voice preview. Check the provider's model access.")
        opening = live_opening_event("Hello! This is a short sample of my voice. What would you like to work on?")
        await send(opening)

        async def silence():
            while True:
                await send({"type": "session.input_audio.append", "audio": base64.b64encode(bytes(3840)).decode()})
                await asyncio.sleep(.08)

        task = asyncio.create_task(silence())
        audio = bytearray()
        last_sound = 0
        caption = False
        try:
            async with asyncio.timeout(14):
                async for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind in ("error", "session.error"):
                        raise ValueError("GPT Live rejected this voice preview. Check the provider's model access.")
                    if kind == "session.instructions.appended" and event.get("client_event_id") == opening["event_id"]:
                        await send({"type": "session.commentary.append", "delegation_id": None,
                                    "content": "Begin the conversation now, following the instructions provided."})
                    elif kind == "session.output_transcript.delta":
                        caption = caption or bool(event.get("delta"))
                    elif kind == "session.output_audio.delta":
                        frame = base64.b64decode(event.get("delta") or "", validate=True)
                        if len(frame) % 2:
                            raise ValueError("GPT Live returned invalid preview audio.")
                        audible = any(abs(sample[0]) > 180 for sample in struct.iter_unpack("<h", frame))
                        if audible or audio:
                            audio.extend(frame)
                        if audible:
                            last_sound = len(audio)
                        # Live has no output-audio-done event. Finish after a
                        # second of trailing silence, bounded by the deadline.
                        if caption and last_sound and len(audio) - last_sound >= 48000:
                            break
        except TimeoutError:
            if not last_sound or not caption:
                raise ValueError("GPT Live did not return a spoken preview. Try again.") from None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Also closes on cancellation/disconnect, releasing billable time.
            try:
                await send({"type": "session.close"})
            except Exception:
                pass
        if not last_sound or not caption:
            raise ValueError("GPT Live did not return a spoken preview. Try again.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio[:last_sound + 9600])
        return output.getvalue()

async def _generate_grok(api_key: str, voice: str) -> bytes:
    from app.core.voice_models import GROK_MODEL, GROK_URL
    from app.core.voice_grok import grok_session
    from app.core.voice_options import validate_options

    async with connect(GROK_URL, additional_headers={"Authorization": f"Bearer {api_key}"},
                       open_timeout=10, close_timeout=2, max_size=2_000_000) as ws:
        session = grok_session({"voice": voice}, validate_options(GROK_MODEL, {}))
        session["session"].update({"tools": [], "turn_detection": None,
                                   "instructions": "Speak only the requested short greeting, then stay quiet."})
        await ws.send(json.dumps(session))
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") == "error":
                raise ValueError("xAI rejected the voice preview settings.")
            if event.get("type") == "session.updated":
                break
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                "Say: Hello! This is a short sample of my voice. What would you like to work on?"}],
        }}))
        await ws.send(json.dumps({"type": "response.create"}))
        audio = bytearray()
        async for raw in ws:
            event = json.loads(raw)
            if event.get("type") in ("response.output_audio.delta", "response.audio.delta"):
                frame = base64.b64decode(event.get("delta") or "", validate=True)
                if len(frame) % 2 or len(audio) + len(frame) > 48_000 * 20:
                    raise ValueError("xAI returned invalid or oversized preview audio.")
                audio.extend(frame)
            elif event.get("type") == "response.done":
                break
            elif event.get("type") == "error":
                raise ValueError("xAI rejected the voice preview.")
        if not audio:
            raise ValueError("xAI returned no preview audio.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio)
        return output.getvalue()
