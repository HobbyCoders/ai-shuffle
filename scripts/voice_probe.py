"""Probe how the real xAI realtime voice model takes the app's notes.

    VOICE_PROBE_XAI_BEARER=... python -m scripts.voice_probe [--shapes all] [--url URL]
    python -m scripts.voice_probe --fake        # against the dev loop's fake provider

The fake provider says every note the app sends, so it cannot show whether the
real model reads a reply aloud, ignores it or rejects the event. This opens
real xAI realtime sessions in a text-driven mode (server turn detection off,
typed user messages, ``response.create`` by hand) and plays one short scripted
exchange per delivery shape, printing for each step any ``error`` event, the
items the provider acknowledged (``conversation.item.added``), the response
status, its transcript and how much audio came with it.

It sends what ships: the real ``LIVE_INSTRUCTIONS`` and session settings
(``grok_session``), ``HANDOFF_TOOL``, the note framing from
``app.core.voice_progress`` and, for the shipped shape, the real
``GrokVoiceStream.send_now``.

Shapes (each in a fresh session):
  old         the handoff output is a receipt, the reply a ``role: "system"`` item, then response.create
  user        the shipped shape: receipt, the reply as a ``role: "user"`` app note, then response.create
  output      the reply as the handoff call's own output, then response.create
  instructions  receipt, then response.create whose ``response.instructions`` carry the reply
  context     a quiet ``role: "system"`` context item, then a typed question that needs it
  unanswered  the caller speaks again while the handoff call has no output yet

The bearer comes from VOICE_PROBE_XAI_BEARER (an xAI API key, or a SuperGrok
access token) and is never printed or logged. Each run makes real, billed
provider calls: a few short responses per shape.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):  # ``python scripts/voice_probe.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN_ENV = "VOICE_PROBE_XAI_BEARER"
SHAPES = ("old", "user", "output", "instructions", "context", "unanswered")
# The receipt before this change (#768/#769), for the "old" shape only.
OLD_RECEIPT = "Handed off. Your reply to the caller follows in the next note."
REQUEST = "Can you check how last night's build went for me?"
# A reply of the size the chat now writes for the ear (about 60 words).
REPLY = ("Last night's build passed except for one flaky test, the upload retry test. It failed once "
         "and passed on the rerun. Everything else is green, and the type check is clean. "
         "Should I mark that test as flaky so it stops blocking merges?")
CONTEXT_FACT = "The nightly build on main finished: 3 tests failed, all in the upload module."
CONTEXT_QUESTION = "How many tests failed last night, and where?"
SIDE_QUESTION = "While that runs, what's two plus two?"


def _connect(url: str, token: str):
    from websockets.asyncio.client import connect
    headers = {"Authorization": "Bearer " + token} if token else {}
    return connect(url, additional_headers=headers, open_timeout=30, max_size=4_000_000)


class Probe:
    """One provider session, reading every event into a log of what came back."""

    def __init__(self, ws, label: str, timeout: float):
        self.ws, self.label, self.timeout = ws, label, timeout
        self.events: asyncio.Queue = asyncio.Queue()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                await self.events.put(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 - reported as the session ending
            await self.events.put({"type": "probe.closed", "detail": type(exc).__name__})
        else:
            await self.events.put({"type": "probe.closed", "detail": "closed"})

    async def send(self, event: dict):
        await self.ws.send(json.dumps(event))

    async def collect(self, until=("response.done",), seconds: float | None = None) -> dict:
        """Read events until one of ``until`` (or the timeout); summarise them."""
        out = {"errors": [], "added": [], "transcript": "", "text": "", "audio_bytes": 0,
               "function_calls": [], "status": None, "ended": None}
        deadline = time.monotonic() + (seconds or self.timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                out["ended"] = "timeout"
                return out
            try:
                event = await asyncio.wait_for(self.events.get(), left)
            except TimeoutError:
                out["ended"] = "timeout"
                return out
            kind = event.get("type")
            if kind == "error":
                error = event.get("error") or {}
                out["errors"].append({k: error.get(k) for k in ("type", "code", "message", "param")})
            elif kind == "conversation.item.added":
                item = event.get("item") or {}
                out["added"].append("/".join(str(item.get(k)) for k in ("type", "role") if item.get(k)))
            elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                out["transcript"] += event.get("delta") or ""
            elif kind in ("response.output_text.delta", "response.text.delta"):
                out["text"] += event.get("delta") or ""
            elif kind in ("response.output_audio.delta", "response.audio.delta"):
                delta = event.get("delta") or ""
                out["audio_bytes"] += len(base64.b64decode(delta)) if delta else 0
            elif kind == "response.function_call_arguments.done":
                out["function_calls"].append({"name": event.get("name"), "call_id": event.get("call_id")})
            elif kind == "response.done":
                out["status"] = (event.get("response") or {}).get("status")
            if kind in until or kind == "probe.closed":
                out["ended"] = kind
                return out

    async def close(self):
        await self.ws.close()
        self.reader.cancel()


def report(label: str, step: str, out: dict):
    print(f"  [{label}] {step}")
    print(f"    errors: {json.dumps(out['errors']) if out['errors'] else 'none'}")
    print(f"    items acknowledged: {', '.join(out['added']) or 'none'}")
    if out["function_calls"]:
        print(f"    function calls: {', '.join(c['name'] or '?' for c in out['function_calls'])}")
    audio_ms = out["audio_bytes"] // 48  # PCM16 mono at 24 kHz
    print(f"    response: status={out['status']} ended_on={out['ended']} audio={out['audio_bytes']} bytes (~{audio_ms} ms)")
    said = (out["transcript"] or out["text"]).strip()
    print(f"    said ({len(said)} chars): {said or '(nothing)'}")


def note_for_reply() -> str:
    """The spoken result note exactly as VoiceProgress.flush builds it."""
    from app.core.voice_progress import (
        NOTE_MAX_TOTAL_BYTES,
        RESULT_NOTE,
        SPOKEN_NOTE_PREFIX,
        _clip,
        speech_text,
    )
    return _clip(SPOKEN_NOTE_PREFIX + RESULT_NOTE + speech_text(REPLY), NOTE_MAX_TOTAL_BYTES)


def session_update(voice: str, modalities: list[str] | None) -> dict:
    from app.core.voice_grok import grok_session
    event = grok_session({"voice": voice}, {"instructions": ""})
    event["session"]["turn_detection"] = {"type": None}  # Typed turns; responses on request.
    if modalities:
        event["session"]["modalities"] = modalities
    return event


def user_text(text: str) -> dict:
    return {"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


async def open_probe(args, label: str) -> Probe:
    if args.fake_arm:
        args.fake_arm(label)
    ws = await _connect(args.url, args.token)
    probe = Probe(ws, label, args.timeout)
    await probe.send(session_update(args.voice, args.modalities))
    out = await probe.collect(until=("session.updated",))
    if out["ended"] != "session.updated":
        report(label, "session.update (rejected or no answer)", out)
        await probe.close()
        raise RuntimeError(f"{label}: the session was not accepted")
    return probe


async def ask_for_handoff(probe: Probe, label: str) -> str | None:
    """The caller's request; the model should call the handoff tool."""
    from app.core.voice_grok import HANDOFF_TOOL
    await probe.send(user_text(REQUEST))
    await probe.send({"type": "response.create"})
    out = await probe.collect()
    report(label, "request (expect a handoff call)", out)
    calls = list(out["function_calls"])
    if not calls:
        # A handoff in a response of its own (the fake provider's, or a model that
        # answers first and calls the tool after).
        calls = (await probe.collect(until=("response.done",), seconds=3))["function_calls"]
    call = next((c["call_id"] for c in calls if c["name"] == HANDOFF_TOOL["name"]), None)
    if call is None:
        print("    the model did not call the handoff tool; this shape cannot be checked")
    return call


async def run_shape(args, shape: str):
    from app.core.voice_grok import HANDOFF_RECEIPT, GrokVoiceStream
    from app.core.voice_models import GROK_MODEL
    from app.core.voice_progress import NOTE_PREFIX
    from app.core.voice_prompts import LIVE_INSTRUCTIONS
    print(f"\n== {shape}")
    probe = await open_probe(args, shape)
    try:
        if shape == "context":
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "system",
                "content": [{"type": "input_text", "text": NOTE_PREFIX + CONTEXT_FACT}]}})
            report(shape, "quiet system context item", await probe.collect(until=("conversation.item.added",), seconds=5))
            await probe.send(user_text(CONTEXT_QUESTION))
            await probe.send({"type": "response.create"})
            out = await probe.collect()
            report(shape, "typed question that needs the context", out)
            said = (out["transcript"] or out["text"]).lower()
            print(f"    used the context: {('3' in said or 'three' in said) and 'upload' in said}")
            return
        call = await ask_for_handoff(probe, shape)
        if call is None:
            return
        note = note_for_reply()
        if shape == "unanswered":
            await probe.send(user_text(SIDE_QUESTION))
            await probe.send({"type": "response.create"})
            report(shape, "caller speaks while the handoff call has no output", await probe.collect())
            return
        if shape == "old":
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call, "output": OLD_RECEIPT}})
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "system", "content": [{"type": "input_text", "text": note}]}})
            await probe.send({"type": "response.create"})
        elif shape == "user":
            # The shipped code path, writing to this socket.
            stream = GrokVoiceStream(None, {"id": "voice-probe"}, {"model": GROK_MODEL, "voice": args.voice}, "", 5)
            stream.upstream = probe.ws
            stream.pending_tools.add(call)
            await stream.send_now({"type": "session.commentary.append", "delegation_id": call, "content": note})
        elif shape == "output":
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call, "output": note}})
            await probe.send({"type": "response.create"})
        elif shape == "instructions":
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call, "output": HANDOFF_RECEIPT}})
            await probe.send({"type": "response.create", "response": {
                "instructions": LIVE_INSTRUCTIONS + "\n\n" + note}})
        out = await probe.collect()
        report(shape, f"reply delivered ({len(note.encode('utf-8'))} bytes of note)", out)
        # A follow-up about the result: answered from it, without a new handoff.
        await probe.send(user_text("Wait, which test was it?"))
        await probe.send({"type": "response.create"})
        report(shape, "follow-up question about the result", await probe.collect())
    finally:
        await probe.close()


def start_fake(args):
    """The dev loop's fake provider on a free loopback port, with a bare handoff scenario."""
    from scripts.devloop.fake_voice import FakeVoice
    directory = Path(tempfile.mkdtemp(prefix="voice-probe-"))
    (directory / "default.json").write_text(json.dumps({"description": "probe default"}), encoding="utf-8")
    (directory / "handoff.json").write_text(json.dumps(
        {"description": "probe: hand off at once", "steps": [{"handoff": True, "ack": ""}]}), encoding="utf-8")
    fake = FakeVoice(directory)
    args.url = fake.url.rstrip("/") + "/v1/realtime?model=grok-voice-latest"
    args.fake_arm = lambda label: fake.arm("default" if label == "context" else "handoff")
    return fake


def parse(argv=None):
    from app.core.voice_models import GROK_URL
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     epilog=f"The bearer is read from {TOKEN_ENV}; it is never printed.")
    parser.add_argument("--url", default=GROK_URL, help="realtime socket URL (default: the shipped xAI URL)")
    parser.add_argument("--shapes", default="all", help=f"comma list of {', '.join(SHAPES)}, or all")
    parser.add_argument("--voice", default="eve")
    parser.add_argument("--modalities", default="", help="optional session modalities, e.g. text")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each response")
    parser.add_argument("--fake", action="store_true", help="run against the dev loop's fake provider (no bearer, no cost)")
    args = parser.parse_args(argv)
    args.shapes = SHAPES if args.shapes == "all" else tuple(s.strip() for s in args.shapes.split(",") if s.strip())
    unknown = [s for s in args.shapes if s not in SHAPES]
    if unknown:
        parser.error(f"unknown shape(s): {', '.join(unknown)}")
    args.modalities = [m.strip() for m in args.modalities.split(",") if m.strip()] or None
    args.fake_arm = None
    return args


async def main_async(args) -> int:
    failed = 0
    for shape in args.shapes:
        try:
            await run_shape(args, shape)
        except Exception as exc:  # noqa: BLE001 - one shape failing must not hide the rest
            failed += 1
            print(f"  [{shape}] probe failed: {type(exc).__name__}: {str(exc)[:200]}")
    return 1 if failed else 0


def main(argv=None) -> int:
    args = parse(argv)
    fake = None
    if args.fake:
        fake = start_fake(args)
        args.token = ""
    else:
        args.token = os.environ.get(TOKEN_ENV, "").strip()
        if not args.token:
            print(f"Set {TOKEN_ENV} to an xAI bearer (API key or SuperGrok access token).", file=sys.stderr)
            return 2
    try:
        return asyncio.run(main_async(args))
    finally:
        if fake is not None:
            fake.close()


if __name__ == "__main__":
    sys.exit(main())
