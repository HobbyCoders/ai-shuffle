"""A chat's latest outcome crosses a real hand-off; its execution rows stay behind (#1032)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.api.sessions_helpers import _annotate_cluster_fields
from app.core import session_runtime
from app.db import database, runs, session_events, turns
from app.network import catalog, control, engine, handoff
from tests.core.network.harness import converge
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.network.test_handoff_receipts import SID, plain
from tests.core.test_checkout_transfer import repositories  # noqa: F401
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import (
    _clean_module_state,  # noqa: F401 — fixture
    _runtime_env,
)


def end_turn(outcome="completed", stop_reason=None, error=None):
    turn = turns.enqueue_turn(SID, "user", prompt="go")
    turns.start_turn(turn["id"])
    return turns.settle_turn(turn["id"], outcome, stop_reason, error=error)


def failed_here():
    return SID in turns.sessions_with_failed_latest_turn([SID])


def wire(monkeypatch, source, target, *, strip=False):
    """Let the receiver reach ``source``; ``strip`` makes that source an older worker."""
    monkeypatch.setattr(handoff.asyncio, "sleep", AsyncMock())

    async def call(device, action, body, **kwargs):
        with source.active():
            return await control.handle(action, body, from_device=target.device_id)

    monkeypatch.setattr(control, "call", call)
    if strip:
        real = handoff._open_export

        def older(*args):
            data, size = real(*args)
            data.pop("last_turn_failed", None)
            return data, size

        monkeypatch.setattr(handoff, "_open_export", older)


async def move(source, target):
    with source.active():
        catalog.publish_owned()
        engine.scan_all()
    converge([source, target])
    with target.active():
        await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == target.device_id
        catalog.publish_owned()
        engine.scan_all()
    converge([source, target])


def seen_from(viewer, owner):
    with viewer.active():
        rows = [dict(database.get_session(SID))]
        rows[0]["last_turn_failed"] = failed_here()
        with patch.object(database, "get_installs_map", return_value={
            owner.device_id: {"device_name": "Owner", "online": True, "approved": True},
            viewer.device_id: {"device_name": "Viewer", "online": True, "approved": True},
        }):
            _annotate_cluster_fields(rows)
    return rows[0]


@pytest.mark.asyncio
async def test_an_idle_failed_chat_still_reads_failed_on_the_worker_it_moved_to(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b)
    with a.active():
        end_turn("aborted", "error", "HTTP 400")
        assert failed_here()

    await move(a, b)

    with b.active():
        assert failed_here()
        # Nothing executable came along: the outcome is all that crossed.
        assert turns.get_latest_run_turn(SID) is None
        assert runs.get_active_run(SID) is None
    assert seen_from(a, b)["last_turn_failed"] is True


@pytest.mark.asyncio
async def test_a_good_turn_on_the_new_owner_clears_the_failure_on_both_workers(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b)
    with a.active():
        end_turn("aborted", "error", "HTTP 400")
    await move(a, b)

    with b.active():
        end_turn()
        assert not failed_here()
        catalog.publish_owned()
        engine.scan_all()
    converge([a, b])

    with a.active():
        assert failed_here(), "the failed turn stays behind here, as history"
    seen = seen_from(a, b)
    assert seen["remote_runtime_state"] == "idle"
    assert seen["last_turn_failed"] is False


@pytest.mark.asyncio
async def test_a_chat_that_comes_back_reads_its_newest_outcome_not_the_turn_it_left_behind(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b)
    with a.active():
        end_turn("aborted", "error", "HTTP 400")
    await move(a, b)
    with b.active():
        end_turn()

    wire(monkeypatch, b, a)
    await move(b, a)

    with a.active():
        assert not failed_here()
        end_turn("aborted", "error", "HTTP 500")
        assert failed_here()


@pytest.mark.asyncio
async def test_a_healthy_chat_moves_as_healthy_over_a_failure_retained_on_the_target(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b)
    with b.active():
        end_turn("aborted", "error", "left here by an earlier stay")
    with a.active():
        end_turn()

    await move(a, b)

    with b.active():
        assert not failed_here()


@pytest.mark.asyncio
async def test_an_older_source_sends_no_outcome_and_the_chat_reads_as_it_did_before(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b, strip=True)
    with a.active():
        end_turn()
    with b.active():
        end_turn("aborted", "error", "left here by an earlier stay")

    await move(a, b)

    with b.active():
        assert not failed_here()
        end_turn("aborted", "error", "HTTP 400")
        assert failed_here()


@pytest.mark.asyncio
async def test_an_older_source_still_leaves_the_failure_its_summary_reported(pair, monkeypatch):
    """A source without the field that did publish a failure: the summary is the word left."""
    a, b, _, _ = pair
    plain(a, b)
    wire(monkeypatch, a, b, strip=True)
    with a.active():
        end_turn("aborted", "error", "HTTP 400")

    await move(a, b)

    with b.active():
        assert failed_here()


def test_an_older_owners_summary_claims_no_failure_on_a_mirror_holding_one(pair):
    a, b, _, _ = pair
    with a.active():
        catalog.publish_owned()
        engine.scan_all()
    converge([a, b])
    # An older owner publishes only ``idle``: the mirror must not fall back on its own row.
    seen = seen_from(b, a)
    assert seen["remote_runtime_state"] == "idle"
    assert seen["last_turn_failed"] is False


@pytest.mark.asyncio
async def test_recovery_of_a_lost_commit_reply_keeps_the_outcome(pair, monkeypatch):
    """The move finished on the source, the receiver restarted: staged, so not lost."""
    a, b, _, _ = pair
    plain(a, b)
    with a.active():
        end_turn("aborted", "error", "HTTP 400")
        carried = failed_here()
        data = {"token": "lost-reply", "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(SID)), "_prepare_history": True}
    with b.active():
        handoff.install_history(SID, data, ".")
        handoff._mark_commit_sent(SID, "lost-reply", a.device_id, True, carried)
        handoff._accept_owner(SID, b.device_id, None, "lost-reply")
        assert failed_here()


@pytest.mark.asyncio
async def test_a_runtime_failure_keeps_its_turn_id_when_the_chat_moves(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    with a.active():
        turn = turns.enqueue_turn(SID, "user", prompt="go")
        create_recorder = session_runtime.TurnRecorder.create_for_run
        with _runtime_env(pump=make_pump([{"type": "error", "message": "HTTP 400"}])) as env, patch.object(
            session_runtime.TurnRecorder, "create_for_run", wraps=create_recorder,
        ):
            assert await session_runtime.SessionRuntime(turn).execute() == "aborted"
        (frame,) = [call.args[1] for call in env["broadcaster"].await_args_list
                    if call.args[1].get("type") == "error"]
        (failure,) = [row for row in session_events.get_events(SID)
                      if row["event_type"] == "turn.aborted"]
        assert failure["turn_id"] == frame["turn_id"] == turn["id"]
        exported = handoff._history(database.get_session(SID))["session_events"]
        (exported_failure,) = [row for row in exported if row["event_type"] == "turn.aborted"]
        assert exported_failure.get("turn_id") == frame["turn_id"]

    wire(monkeypatch, a, b)
    await move(a, b)

    with b.active():
        (received_failure,) = [row for row in session_events.get_events(SID)
                               if row["event_type"] == "turn.aborted"]
        assert received_failure["turn_id"] == frame["turn_id"]
        assert turns.get_turn(turn["id"]) is None  # Correlation is history, not execution.


@pytest.mark.parametrize("legacy", ["source", "staging"])
def test_a_history_move_without_the_new_turn_id_field_still_publishes(mock_db, legacy):
    from psycopg2.extras import Json
    from app.db.base import get_db, parse_jsonb

    sid = "legacy-error-correlation"
    database.create_session(sid, profile_id=None)
    session_events.append_events(sid, [{
        "type": "turn.aborted", "turn_id": "old-turn", "payload": {"reason": "error"},
    }])
    data = {"token": "old-move", "sdk_session_id": None, "_prepare_history": True,
            "history": handoff._history(database.get_session(sid))}
    if legacy == "source":
        for row in data["history"]["session_events"]:
            row.pop("turn_id")
    handoff.install_history(sid, data, ".")
    with get_db() as conn:
        cur = conn.cursor()
        if legacy == "staging":
            cur.execute("SELECT rows FROM handoff_history_chunks WHERE token=%s AND table_name=%s",
                        ("old-move", "session_events"))
            old_shape = [values[:-1] for values in parse_jsonb(cur.fetchone()["rows"])]
            cur.execute("UPDATE handoff_history_chunks SET rows=%s WHERE token=%s AND table_name=%s",
                        (Json(old_shape), "old-move", "session_events"))
        handoff._publish_history(cur, sid, "old-move")
    (failure,) = session_events.get_events(sid)
    assert failure["event_type"] == "turn.aborted"
    assert failure["turn_id"] is None  # Older senders never supplied an identity.
