"""The dev loop's fake voice provider: both wire dialects over real sockets.

The app's own transports (GPT Live ``VoiceStream``, ``GrokVoiceStream``) send
notes to it, so the log shows what the app asked the voice to say.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from scripts.devloop import fake_voice
from scripts.devloop.fake_voice import (
    SCENARIOS_DIR,
    FakeVoice,
    ScenarioError,
    Scenarios,
    parse_tag,
    spoken_words,
    validate_scenario,
)

VOICED = base64.b64encode(b"\x10\x02" * 4800).decode()  # 200 ms of non-silent PCM16


@pytest.fixture
def fake(tmp_path: Path):
    with FakeVoice(log_path=tmp_path / "voice.jsonl") as server:
        yield server


async def _drain(ws) -> None:
    """Read what the fake says, as the app does (it would block on a full socket)."""
    async for _ in ws:
        pass


async def _wait_for(fake: FakeVoice, kind: str, count: int = 1, timeout: float = 5.0, **match) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        found = [e for e in fake.since(0) if e["kind"] == kind
                 and all(e.get(k) == v for k, v in match.items())]
        if len(found) >= count:
            return found
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no {count}× {kind} {match} in {fake.since(0)}")
        await asyncio.sleep(0.02)


def test_markers_match_the_app() -> None:
    from app.core import voice_grok, voice_progress, voice_stream

    assert fake_voice.SPOKEN_NOTE_PREFIX == voice_progress.SPOKEN_NOTE_PREFIX
    assert fake_voice.NOTE_PREFIX == voice_progress.NOTE_PREFIX
    assert voice_progress.RESULT_NOTE.endswith(fake_voice.REPLY_MARKERS[1])
    assert voice_progress.MILESTONE_NOTE.endswith(fake_voice.REPLY_MARKERS[0])
    assert voice_stream.RESUME_NOTE.startswith(fake_voice.RESUME_MARKER)
    assert fake_voice.HANDOFF_TOOL == voice_grok.HANDOFF_TOOL["name"]
    # The commentary GPT Live gets after its greeting trigger is acknowledged.
    assert "Begin the conversation now" in Path(voice_stream.__file__).read_text()


def test_shipped_scenarios_all_load() -> None:
    assert set(Scenarios(SCENARIOS_DIR).load()) >= {
        "default", "relay", "wait-update", "busy", "inactivity", "renew", "error", "drop"}


@pytest.mark.parametrize("data", [{"steps": [{}]}, {"steps": [{"caller": "a", "handoff": True}]},
                                  {"steps": "x"}, []])
def test_bad_scenarios_are_refused(data) -> None:
    with pytest.raises(ScenarioError):
        validate_scenario("bad", data)


def test_tag_and_spoken_words() -> None:
    assert parse_tag("User: hi\nUser: [voice:relay reply_delay_ms=5] tok\nAssistant: ok") == (
        "relay", {"reply_delay_ms": "5"}, "User: [voice:relay reply_delay_ms=5] tok")
    assert parse_tag("no tag")[0] is None
    from app.core.voice_progress import RESULT_NOTE, SPOKEN_NOTE_PREFIX
    assert spoken_words(SPOKEN_NOTE_PREFIX + RESULT_NOTE + "It passed. Ship it?") == "It passed. Ship it?"
    assert spoken_words(SPOKEN_NOTE_PREFIX + "Tell them to check the chat.") == "Tell them to check the chat."


def test_http_controls(fake: FakeVoice) -> None:
    base = f"http://127.0.0.1:{fake.port}"
    assert httpx.get(base + "/v1/models/gpt-live-1.1").status_code == 404
    assert "relay" in httpx.get(base + "/fake/scenarios").json()
    assert httpx.get(base + "/fake/arm?scenario=nope").status_code == 404
    assert httpx.get(base + "/fake/arm?scenario=error").json() == {"armed": "error"}
    entries = httpx.get(base + "/fake/log").json()
    assert entries[-1]["kind"] == "armed"
    assert httpx.get(base + f"/fake/log?since={entries[-1]['seq']}").json() == []


def test_the_fake_binds_only_loopback_or_everything(tmp_path) -> None:
    with pytest.raises(ValueError):
        FakeVoice(host="10.0.0.5")


def test_live_relay_scenario_plays_after_microphone_audio(fake: FakeVoice) -> None:
    async def run():
        async with connect(fake.url + "/v1/live/sessions") as ws:
            await ws.send(json.dumps({"type": "session.start", "session": {
                "model": "gpt-live-1", "instructions": "x", "audio": {"output": {"voice": "marin"}},
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "User: [voice:relay word_ms=0] tok1"}]}]}}))
            assert json.loads(await ws.recv())["type"] == "session.started"
            await ws.send(json.dumps({"type": "session.input_audio.append", "audio": VOICED}))
            await ws.send(json.dumps({"type": "session.input_audio.append", "audio": VOICED}))
            kinds, caller = [], ""
            while "session.delegation.created" not in kinds:
                event = json.loads(await asyncio.wait_for(ws.recv(), 5))
                kinds.append(event["type"])
                if event["type"] == "session.input_transcript.delta":
                    caller += event["delta"]
            assert caller == "Can you check the build for me? [script:voice-reply]"
        await _wait_for(fake, "speech", trigger="ack", text="Let me check.")
        await _wait_for(fake, "session.closed")

    asyncio.run(run())
    assert [e["name"] for e in fake.since(0) if e["kind"] == "scenario"] == ["relay"]
    assert any(e["kind"] == "audio" and e["heard"] for e in fake.since(0))


def test_gpt_live_speaks_a_split_note_once_in_full(fake: FakeVoice, monkeypatch) -> None:
    """VoiceStream sends a long result as several appends: one reply, all of it."""
    from app.core.voice_progress import RESULT_NOTE, SPOKEN_NOTE_PREFIX, split_bytes
    from app.core.voice_stream import VoiceStream

    reply = " ".join(f"Point {n} is done and checked." for n in range(1, 30))
    note = SPOKEN_NOTE_PREFIX + RESULT_NOTE + reply

    async def run():
        async with connect(fake.url + "/v1/live/sessions") as ws:
            reader = asyncio.create_task(_drain(ws))
            stream = VoiceStream.__new__(VoiceStream)
            stream.closed, stream.restoring, stream.config = False, False, {"model": "gpt-live-1"}
            stream.stale_delegations, stream.upstream = {}, ws
            pieces = split_bytes(note, 400)
            assert len(pieces) > 2
            for piece in pieces:
                await stream.send({"type": "session.commentary.append", "delegation_id": "d1", "content": piece})
            await _wait_for(fake, "speech", trigger="note")
            await asyncio.sleep(0.3)
            reader.cancel()

    asyncio.run(run())
    speeches = [e for e in fake.since(0) if e["kind"] == "speech"]
    assert [s["text"] for s in speeches] == [reply]
    assert speeches[0]["pieces"] > 2


def test_grok_says_a_reply_note_once_not_as_the_handoff_output(fake: FakeVoice) -> None:
    """GrokVoiceStream closes the handoff with a receipt and speaks the note (#768, #732)."""
    from app.core.voice_grok import HANDOFF_RECEIPT, GrokVoiceStream
    from app.core.voice_progress import RESULT_NOTE

    reply = "I checked it. " * 60

    async def run():
        async with connect(fake.url + "/v1/realtime?model=grok-voice-latest") as ws:
            reader = asyncio.create_task(_drain(ws))
            stream = GrokVoiceStream.__new__(GrokVoiceStream)
            stream.closed, stream.restoring, stream.upstream = False, False, ws
            stream.pending_tools = {"call_1"}
            stream.response_active = stream.speech_active = stream.speech_pending = False
            await stream.send({"type": "session.commentary.append", "delegation_id": "call_1",
                               "content": RESULT_NOTE + reply})
            await _wait_for(fake, "speech", trigger="note")
            reader.cancel()

    asyncio.run(run())
    log = fake.since(0)
    assert [e["text"] for e in log if e["kind"] == "tool_output"] == [HANDOFF_RECEIPT]
    assert [e["text"] for e in log if e["kind"] == "speech"] == [reply.strip()]
    assert sum(e["kind"] == "response.create" for e in log) == 1
