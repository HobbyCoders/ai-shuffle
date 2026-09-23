"""
Regression tests: execute_query() must link sessions.sdk_session_id as soon
as the SDK's `init` message announces it — not only at end-of-turn.

execute_query is the non-streaming path used by automations. Its transcript
entries flow into the durable store live (mirror batcher), but the history
read path (GET /{session_id}) looks those entries up by
``sessions.sdk_session_id`` — which _persist_turn_results only wrote at turn
end. Loading a running automation session therefore showed ONLY the user
prompt for the entire run (blank "no work" history), and a mid-turn crash
orphaned the session permanently (blank until the orphan reconciler guessed
a relink). The streaming and WebSocket paths already persist the link at
init; execute_query was the one path that didn't.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# execute_query consumes the provider MIRROR vocabulary (it runs through
# adapter.receive_events(), which already translated), not the raw SDK types.
from app.core.provider.events import SystemMessage

import app.core.query_engine as qe

SDK_SESSION_ID = "0376d4af-2a63-43d2-883c-91c50aba7b78"
APP_SESSION_ID = "b19ddfa6-9357-4227-9c2f-a6b2f00c3b82"


def _init_message():
    return SystemMessage(subtype="init", data={"session_id": SDK_SESSION_ID})


def _adapter_patch(events_factory):
    """Patch the provider factory so execute_query gets a stub adapter whose
    receive_events() replays ``events_factory()`` (an async generator)."""
    adapter = MagicMock()
    adapter.connect_with_sdk_options = AsyncMock()
    adapter.send_query = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.receive_events = MagicMock(side_effect=lambda: events_factory())
    return patch(
        "app.core.provider.factory.make_adapter", return_value=adapter
    )


def _early_link_calls(mock_db):
    """update_session calls that carry ONLY the sdk_session_id linkage."""
    return [
        c
        for c in mock_db.update_session.call_args_list
        if c.kwargs.get("sdk_session_id") == SDK_SESSION_ID
        and "cost_increment" not in c.kwargs
    ]


@pytest.fixture()
def engine_mocks():
    profile = {"id": "principal-engineer", "name": "PE"}
    session_row = {"id": APP_SESSION_ID, "sdk_session_id": None}
    options = MagicMock()
    options.cwd = None

    async def _noop(*args, **kwargs):
        return None

    with patch.object(qe, "get_profile", return_value=profile), patch.object(
        qe, "database"
    ) as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qe, "resolve_note_references", side_effect=lambda p: p
    ), patch.object(
        qe, "reconcile_memory", _noop
    ), patch.object(
        qe, "push_memory", _noop
    ):
        mock_db.get_session.return_value = session_row
        yield mock_db


@pytest.mark.asyncio
async def test_crash_after_init_still_links_sdk_session_id(engine_mocks):
    """A mid-turn crash must not orphan the session (blank history)."""

    async def crashing_events():
        yield _init_message()
        raise RuntimeError("subprocess died mid-turn")

    with _adapter_patch(crashing_events):
        with pytest.raises(RuntimeError, match="mid-turn"):
            await qe.execute_query(
                prompt="do the thing",
                profile_id="principal-engineer",
                session_id=APP_SESSION_ID,
            )

    assert _early_link_calls(engine_mocks), (
        "sdk_session_id was never persisted before the crash — the session "
        "is orphaned and its history reads back empty"
    )


@pytest.mark.asyncio
async def test_link_persists_at_init_before_turn_end(engine_mocks):
    """During a long run the link must exist BEFORE _persist_turn_results."""
    linked_before_result = []

    async def slow_events():
        yield _init_message()
        # By the time the next message arrives, the link must be durable.
        linked_before_result.append(bool(_early_link_calls(engine_mocks)))

    with _adapter_patch(slow_events), patch.object(
        qe, "_persist_turn_results", MagicMock(side_effect=lambda **kw: _noop_coro())
    ):
        await qe.execute_query(
            prompt="do the thing",
            profile_id="principal-engineer",
            session_id=APP_SESSION_ID,
        )

    assert linked_before_result == [True]


@pytest.mark.asyncio
async def test_resumed_session_skips_redundant_early_link(engine_mocks):
    """Resumes already have the link — no extra mid-turn write."""
    engine_mocks.get_session.return_value = {
        "id": APP_SESSION_ID,
        "sdk_session_id": SDK_SESSION_ID,
    }

    async def quick_events():
        yield _init_message()

    with _adapter_patch(quick_events), patch.object(
        qe, "_persist_turn_results", MagicMock(side_effect=lambda **kw: _noop_coro())
    ):
        await qe.execute_query(
            prompt="again",
            profile_id="principal-engineer",
            session_id=APP_SESSION_ID,
        )

    assert not _early_link_calls(engine_mocks)


@pytest.mark.asyncio
async def test_resume_repersists_launched_model_on_model_switch(engine_mocks):
    """A fresh-subprocess RESUME that relaunches with a DIFFERENT effective
    model must re-persist sessions.launched_model at init — even though the
    sdk_session_id link itself stays resume-gated.

    The Unraid host-op guard reads sessions.launched_model with precedence
    over the transcript's msg_model. When a pooled client is evicted/dead (or
    requires_reconnect) the resume spawns a fresh subprocess with whatever
    model options carry now; if that model differs from the last launch and we
    skip the write on the resume path, the guard keeps reading a STALE model —
    a transient Fable->non-Fable FAIL-OPEN in the Fable-only guard.
    """
    # Resume path: the session already carries an sdk_session_id.
    engine_mocks.get_session.return_value = {
        "id": APP_SESSION_ID,
        "sdk_session_id": SDK_SESSION_ID,
    }

    resumed_options = MagicMock()
    resumed_options.cwd = None
    # The fresh subprocess relaunches with a different effective model.
    resumed_options.model = "claude-opus-4-1"

    async def quick_events():
        yield _init_message()

    with _adapter_patch(quick_events), patch("app.core.sdk_options.build_options_from_profile", return_value=(resumed_options, {})
    ), patch.object(
        qe, "_persist_turn_results", MagicMock(side_effect=lambda **kw: _noop_coro())
    ):
        await qe.execute_query(
            prompt="again",
            profile_id="principal-engineer",
            session_id=APP_SESSION_ID,
        )

    model_writes = [
        c
        for c in engine_mocks.update_session.call_args_list
        if c.kwargs.get("launched_model") == "claude-opus-4-1"
    ]
    assert model_writes, (
        "resume-path init never re-persisted launched_model — the host-op "
        "guard reads a stale model (Fable->non-Fable FAIL-OPEN)"
    )
    # The sdk_session_id link stays resume-gated: the model re-persist must NOT
    # drag a redundant sdk_session_id re-link along with it.
    for c in model_writes:
        assert "sdk_session_id" not in c.kwargs, (
            "resume must not re-link sdk_session_id on the model re-persist"
        )


async def _noop_coro():
    return None
