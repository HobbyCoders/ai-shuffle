"""A ``resume``-source turn re-runs the loop from the head, adding no message.

Two seams are covered:

* the pump (``_stream_to_websocket_impl``) sends ``send_resume`` instead of
  ``send_query`` and skips every step that reads or rewrites the prompt — the
  harness drives the REAL generator over a faked adapter, mirroring
  tests/core/test_model_info_event.py;
* ``SessionRuntime._run_pump`` derives ``resume`` from the turn's source and
  tells the recorder to append ``turn.started`` WITHOUT a ``message.user``.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.query_engine as qe
from app.core import session_runtime
from app.core.provider.events import SystemMessage
from app.core.session_pool import SessionState
from app.db import database
from app.db import turns as turns_db

from tests.core.test_session_runtime import _make_pump, _runtime_env


# ─── pump harness (subprocess/profile boundaries only) ───────────────────────


def _make_state():
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()
    adapter.send_resume = AsyncMock()
    return SessionState(adapter=adapter, is_connected=True)


class _StubPool:
    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self, lane="delegated"):
        return None

    async def evict_if_needed(self):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


def _engine_patches(stack, pool, cwd, notes):
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "default", "config": {}}
    ))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = None
    options.aih_provider_id = None
    stack.enter_context(patch(
        "app.core.sdk_options.build_options_from_profile",
        return_value=(options, {}),
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", new=notes))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=AsyncMock()))


def _stream_response(messages):
    async def _gen():
        for m in messages:
            yield m

    return MagicMock(side_effect=lambda: _gen())


async def _drive(prompt, *, resume, tmp_path):
    """Run the real pump once and return (adapter, resolve_note_references spy)."""
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id="default")
    state = _make_state()
    pool = _StubPool(state)
    state.adapter.receive_events = _stream_response(
        [SystemMessage(subtype="init", data={"session_id": "ses_x"})]
    )
    notes = MagicMock(side_effect=lambda p: p)
    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path), notes)
        agen = qe._stream_to_websocket_impl(
            prompt=prompt,
            session_id=session_id,
            profile_id="default",
            resume=resume,
        )
        async for _ in agen:
            pass
    return state.adapter, notes


# ─── pump dispatch ───────────────────────────────────────────────────────────


async def test_resume_sends_resume_and_never_a_query(mock_db, tmp_path):
    adapter, notes = await _drive("", resume=True, tmp_path=tmp_path)

    adapter.send_resume.assert_awaited_once_with()
    adapter.send_query.assert_not_called()
    # Nothing prompt-derived runs: there is no prompt to resolve.
    notes.assert_not_called()


async def test_a_normal_turn_still_sends_a_query(mock_db, tmp_path):
    adapter, notes = await _drive("hi", resume=False, tmp_path=tmp_path)

    adapter.send_query.assert_awaited_once_with("hi")
    adapter.send_resume.assert_not_called()
    notes.assert_called_once_with("hi")


# ─── SessionRuntime wiring ───────────────────────────────────────────────────


async def test_run_pump_passes_resume_and_records_no_user_message(mock_db):
    """A ``resume`` turn starts the recorder with ``record_user=False``."""
    turn = turns_db.enqueue_turn("s-resume", "resume", prompt="")
    seen: dict = {}

    async def _pump(**kwargs):
        seen.update(kwargs)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_pump) as env:
        await session_runtime.SessionRuntime(turn).execute()

    assert seen["resume"] is True
    assert seen["prompt"] == ""
    env["recorder"].start.assert_awaited_once()
    assert env["recorder"].start.call_args.kwargs["record_user"] is False


async def test_run_pump_records_the_user_message_for_a_normal_turn(mock_db):
    turn = turns_db.enqueue_turn("s-user", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()

    env["recorder"].start.assert_awaited_once()
    assert env["recorder"].start.call_args.kwargs["record_user"] is True
