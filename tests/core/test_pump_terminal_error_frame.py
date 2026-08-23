"""The pump surfaces an ``is_error`` result that carries errors, even late.

The fluid legs deliver every provider failure as a terminal ``ResultMessage``
with ``errors`` — the generator ends cleanly, so ``crashed`` never flips. Any
content already forwarded this turn (a ``provider_retry`` note, a model
banner, a text block) used to mark the turn "visible" and downgrade the
failure to a ``done`` frame, which hid it from the transcript and left the
retry card spinning forever.

The harness drives the REAL ``_stream_to_websocket_impl`` over a faked
adapter, reusing the boundary patches from tests/core/test_resume_turn.py.
"""

import uuid
from contextlib import ExitStack

from app.core import query_engine as qe
from app.core.provider.events import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from app.db import database

from tests.core.test_resume_turn import (
    _StubPool,
    _engine_patches,
    _make_state,
    _stream_response,
)

_GIVE_UP = "provider returned an empty response 11 times in a row; giving up"


def _retry_note(attempt: int) -> SystemMessage:
    return SystemMessage(
        subtype="provider_retry",
        data={
            "attempt": attempt,
            "max_attempts": 10,
            "reason": "empty response",
            "delay_seconds": 1,
            "model": "grok-4.6",
        },
    )


async def _frames(messages, tmp_path):
    """Run the real pump over ``messages`` and return every yielded frame."""
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id="default")
    state = _make_state()
    pool = _StubPool(state)
    state.adapter.receive_events = _stream_response(
        [SystemMessage(subtype="init", data={"session_id": "ses_x"})] + list(messages)
    )
    out = []
    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path), lambda p: p)
        agen = qe._stream_to_websocket_impl(
            prompt="hi",
            session_id=session_id,
            profile_id="default",
        )
        async for frame in agen:
            out.append(frame)
    return out


async def test_error_result_after_retry_notes_yields_error_not_done(mock_db, tmp_path):
    """Exhausted retries end the turn on an ``error`` frame, never ``done``."""
    frames = await _frames(
        [
            _retry_note(9),
            _retry_note(10),
            ResultMessage(
                subtype="error_during_execution",
                is_error=True,
                errors=[_GIVE_UP],
                session_id="ses_x",
            ),
        ],
        tmp_path,
    )

    errors = [f for f in frames if f.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["message"] == _GIVE_UP
    assert not [f for f in frames if f.get("type") == "done"]


async def test_error_result_after_text_yields_error_not_done(mock_db, tmp_path):
    """A provider failure after rendered assistant text is still a failure."""
    frames = await _frames(
        [
            AssistantMessage(content=[TextBlock(text="partial answer")], model="grok-4.6"),
            ResultMessage(
                subtype="error_during_execution",
                is_error=True,
                errors=["provider connection reset"],
                session_id="ses_x",
            ),
        ],
        tmp_path,
    )

    errors = [f for f in frames if f.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["message"] == "provider connection reset"
    assert not [f for f in frames if f.get("type") == "done"]


async def test_clean_result_after_retry_notes_still_yields_done(mock_db, tmp_path):
    """Regression guard: retries that eventually succeed complete normally."""
    frames = await _frames(
        [
            _retry_note(1),
            _retry_note(2),
            AssistantMessage(content=[TextBlock(text="the answer")], model="grok-4.6"),
            ResultMessage(subtype="success", is_error=False, session_id="ses_x"),
        ],
        tmp_path,
    )

    assert [f for f in frames if f.get("type") == "done"]
    assert not [f for f in frames if f.get("type") == "error"]
