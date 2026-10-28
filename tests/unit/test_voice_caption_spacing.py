"""Keep Grok response/item captions separate without splitting word pieces."""
import base64
import json
from unittest.mock import AsyncMock

import pytest

from app.core.voice_grok import GrokVoiceStream
from app.core.voice_models import GROK_MODEL
from tests.unit.test_voice_stream import Upstream, stream


def grok_stream(events):
    call = GrokVoiceStream(AsyncMock(), {"id": "s1"},
                           {"model": GROK_MODEL, "voice": "eve"}, "secret", 5)
    call.upstream = Upstream([json.dumps(event) for event in events])
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "response.output_audio_transcript.delta", "response.audio_transcript.delta",
])
@pytest.mark.parametrize("first_ids,second_ids", [
    ({"response_id": "r1"}, {"response_id": "r2"}),
    ({"item_id": "i1"}, {"item_id": "i2"}),
    ({"response_id": "r1", "item_id": "i1"}, {"response_id": "r1", "item_id": "i2"}),
    ({"response_id": "r1", "item_id": "i1"}, {"response_id": "r2", "item_id": "i1"}),
])
@pytest.mark.parametrize("first,second,expected", [
    ("Will do.", "Good news", "Will do. Good news"),
    ("Will do.", "good news", "Will do. good news"),
    ("Will do.", '"Good news"', 'Will do. "Good news"'),
    ("Will do", "Good news", "Will do Good news"),
    ("Will do. ", "Good news", "Will do. Good news"),
    ("Will do.", " Good news", "Will do. Good news"),
])
async def test_grok_assistant_caption_boundaries_reach_display_and_storage(
    monkeypatch, kind, first_ids, second_ids, first, second, expected,
):
    from app.core import turn_recorder

    record = AsyncMock()
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", record)
    call = grok_stream([
        {"type": kind, "delta": first, **first_ids},
        {"type": kind, "delta": second, **second_ids},
    ])
    await call.receive_upstream()
    assert call.pending["assistant"] == expected
    assert "".join(entry["text"] for entry in call.transcript) == expected
    shown = "".join(args.args[0]["delta"] for args in call.websocket.send_json.await_args_list)
    assert shown == expected
    await call.flush()
    record.assert_awaited_once()
    assert record.await_args.args == ("s1", None, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [
    {}, {"response_id": "r1"}, {"item_id": "i1"}, {"response_id": "r1", "item_id": "i1"},
])
async def test_grok_word_piece_deltas_in_one_response_still_join(ids):
    pieces = ("Goo", "d ne", "ws")
    call = grok_stream([
        {"type": "response.output_audio_transcript.delta", "delta": delta, **ids}
        for delta in pieces
    ])
    await call.receive_upstream()
    assert call.pending["assistant"] == "Good news"
    assert [args.args[0]["delta"] for args in call.websocket.send_json.await_args_list] == list(pieces)


@pytest.mark.asyncio
async def test_missing_ids_do_not_turn_word_pieces_into_new_responses():
    call = grok_stream([
        {"type": "response.output_audio_transcript.delta", "delta": "Goo", "response_id": "r1"},
        {"type": "response.output_audio_transcript.delta", "delta": "d ne"},
        {"type": "response.output_audio_transcript.delta", "delta": "ws", "response_id": "r1", "item_id": "i1"},
    ])
    await call.receive_upstream()
    assert call.pending["assistant"] == "Good news"


@pytest.mark.asyncio
async def test_caption_boundary_spacing_does_not_change_spoken_audio():
    audio = b"\x01\x00\x02\x00"
    call = grok_stream([
        {"type": "response.output_audio_transcript.delta", "delta": "Will do.", "response_id": "r1"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        {"type": "response.output_audio_transcript.delta", "delta": "good news", "response_id": "r2"},
    ])
    await call.receive_upstream()
    assert call.pending["assistant"] == "Will do. good news"
    call.websocket.send_bytes.assert_awaited_once_with(audio)


@pytest.mark.asyncio
async def test_shared_live_caption_path_preserves_response_boundaries():
    call = stream()
    call.upstream = Upstream([json.dumps(event) for event in [
        {"type": "session.output_transcript.delta", "delta": "Will do.", "response_id": "r1"},
        {"type": "session.output_transcript.delta", "delta": "good news", "response_id": "r2"},
    ]])
    await call.receive_upstream()
    assert call.pending["assistant"] == "Will do. good news"


@pytest.mark.asyncio
async def test_response_ids_do_not_change_user_caption_word_pieces():
    call = stream()
    call.upstream = Upstream([json.dumps(event) for event in [
        {"type": "session.input_transcript.delta", "delta": "Goo", "response_id": "r1", "item_id": "i1"},
        {"type": "session.input_transcript.delta", "delta": "d news", "response_id": "r2", "item_id": "i2"},
    ]])
    await call.receive_upstream()
    assert call.pending["user"] == "Good news"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "assistant"])
async def test_existing_sentence_and_decimal_caption_joins_are_unchanged(role):
    call = stream()
    for delta in ("The site.", "I've updated to version 3.", "5."):
        await call.caption(role, delta)
    assert call.pending[role] == "The site. I've updated to version 3.5."
