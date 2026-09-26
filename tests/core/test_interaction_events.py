"""Durable ``interaction.*`` lifecycle event emission (#653 PR-A).

Two layers:

* the emitter helpers in ``app.core.interaction_events`` in isolation — event
  shape, unfenced append (``run_id=None``), backfill-before-append ordering, and
  the swallow-on-failure isolation invariant;
* the wired handler sites (``permission_handler`` / ``user_question_handler``),
  always active (durable placement permanently on, #603) — exactly-once
  emission gated on the guarded UPDATE, and lost-race emitting nothing.

Runs on the real pg-shim SQLite DB (``mock_db``). The gateway LISTEN thread and
mobile push are patched off so the suite never spins a real listener/notifier.
"""

import asyncio

from unittest.mock import AsyncMock

from app.core import interaction_events
from app.core.permission_handler import PermissionHandler
from app.core.user_question_handler import UserQuestionHandler
from app.db import interactions
from app.db import session_events as session_events_db
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _events_of_type(session_id, event_type):
    return [
        e for e in session_events_db.get_events(session_id)
        if e.get("event_type") == event_type
    ]


async def _wait_events(session_id, event_type, count, timeout=2.0):
    """Poll until at least ``count`` events of ``event_type`` exist.

    The ``interaction.requested`` emit runs inside the still-blocked
    request_permission/request_answers task (after the row is persisted), so a
    read immediately after ``_wait_pending`` can race the append.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rows = _events_of_type(session_id, event_type)
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.01)
    return _events_of_type(session_id, event_type)


async def _wait_pending(session_id, request_id, kind="permission", timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for r in interactions.get_pending_interactions(session_id):
            if r.get("kind") == kind and (r.get("payload") or {}).get("request_id") == request_id:
                return r
        await asyncio.sleep(0.01)
    raise AssertionError(f"pending {kind} row for {request_id} never appeared")


# ---------------------------------------------------------------------------
# Emitter helpers in isolation
# ---------------------------------------------------------------------------

async def test_emit_requested_appends_unfenced_event(mock_db):
    await interaction_events.emit_interaction_requested(
        "s1", "int-1", "req-1", "permission", {"request_id": "req-1", "tool_name": "Bash"},
    )
    rows = _events_of_type("s1", "interaction.requested")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["interaction_id"] == "int-1"
    assert payload["request_id"] == "req-1"
    assert payload["kind"] == "permission"
    assert payload["request"]["tool_name"] == "Bash"
    # Unfenced append: no owning run lease.
    assert rows[0]["run_id"] is None


async def test_emit_answered_appends_event(mock_db):
    await interaction_events.emit_interaction_answered(
        "s1", "int-1", "req-1", {"decision": "allow"},
    )
    rows = _events_of_type("s1", "interaction.answered")
    assert len(rows) == 1
    assert rows[0]["payload"]["request_id"] == "req-1"
    assert rows[0]["payload"]["answer"]["decision"] == "allow"
    assert rows[0]["run_id"] is None


async def test_emit_expired_carries_reason(mock_db):
    await interaction_events.emit_interaction_expired(
        "s1", "int-1", "req-1", "cancelled",
    )
    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 1
    assert rows[0]["payload"]["reason"] == "cancelled"
    assert rows[0]["payload"]["request_id"] == "req-1"


async def test_emit_runs_backfill_before_append(mock_db, monkeypatch):
    """A first-writer interaction event must not seed the counters before the
    legacy backfill has run — ensure_backfilled is awaited FIRST."""
    order = []

    from app.core import history_backfill

    async def _spy_backfill(session_id):
        order.append("backfill")

    orig_append = session_events_db.append_events

    def _spy_append(session_id, events, *a, **k):
        order.append("append")
        return orig_append(session_id, events, *a, **k)

    monkeypatch.setattr(history_backfill, "ensure_backfilled", _spy_backfill)
    monkeypatch.setattr(session_events_db, "append_events", _spy_append)

    await interaction_events.emit_interaction_requested(
        "s1", "int-1", "req-1", "permission", {},
    )
    assert order == ["backfill", "append"]


async def test_emit_swallows_append_failure(mock_db, monkeypatch):
    """An append failure must never propagate to the caller."""
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_events_db, "append_events", _boom)

    # Must not raise.
    await interaction_events.emit_interaction_answered("s1", "int-1", "req-1", {})
    # And nothing was recorded.
    assert session_events_db.get_events("s1") == []


# ---------------------------------------------------------------------------
# Permission handler — exactly-once answered / expired
# ---------------------------------------------------------------------------

async def test_permission_request_emits_requested(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    rows = await _wait_events("s1", "interaction.requested", 1)
    assert len(rows) == 1
    assert rows[0]["payload"]["request_id"] == "r1"
    assert rows[0]["payload"]["kind"] == "permission"

    await h.respond("r1", "s1", "allow", broadcast_func=broadcast)
    await asyncio.wait_for(task, 12.0)


async def test_permission_answer_emits_once_second_answer_nothing(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    result = await h.respond("r1", "s1", "allow", broadcast_func=broadcast)
    assert result["resolved"] is True
    await asyncio.wait_for(task, 12.0)

    rows = _events_of_type("s1", "interaction.answered")
    assert len(rows) == 1
    assert rows[0]["payload"]["request_id"] == "r1"
    assert rows[0]["payload"]["answer"]["decision"] == "allow"

    # Second answer attempt: the guarded UPDATE finds no pending row → nothing.
    second = await h.respond("r1", "s1", "allow", broadcast_func=broadcast)
    assert second == {"error": "Request not found"}
    assert len(_events_of_type("s1", "interaction.answered")) == 1


async def test_permission_cancel_emits_expired_once_lost_race_nothing(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    assert await h.cancel_request("r1", "s1") is True
    await asyncio.wait_for(task, 12.0)

    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 1
    assert rows[0]["payload"]["reason"] == "cancelled"
    assert rows[0]["payload"]["request_id"] == "r1"

    # Lost race: already cancelled → guarded UPDATE returns False → nothing.
    assert await h.cancel_request("r1", "s1") is False
    assert len(_events_of_type("s1", "interaction.expired")) == 1


async def test_permission_defer_emits_answered(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    res = await h.respond("r1", "s1", "defer", broadcast_func=broadcast)
    assert res["deferred"] is True
    await asyncio.wait_for(task, 12.0)

    rows = _events_of_type("s1", "interaction.answered")
    assert len(rows) == 1
    assert rows[0]["payload"]["answer"]["decision"] == "defer"

    # Requeue mints a fresh pending row → a second interaction.requested.
    await h.requeue_deferred("s1", broadcast_func=broadcast)
    requested = await _wait_events("s1", "interaction.requested", 2)
    assert len(requested) == 2
    assert {r["payload"]["request_id"] for r in requested} == {"r1"}


async def test_permission_cancel_all_emits_expired_per_row(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    tasks = []
    for rid in ("r1", "r2"):
        tasks.append(asyncio.create_task(h.request_permission(
            request_id=rid, session_id="s1", profile_id="default",
            tool_name="Bash", tool_input={}, broadcast_func=broadcast,
        )))
        await _wait_pending("s1", rid)

    assert await h.cancel_all_requests("s1") == 2
    for t in tasks:
        await asyncio.wait_for(t, 12.0)

    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 2
    assert all(r["payload"]["reason"] == "cancelled" for r in rows)
    assert {r["payload"]["request_id"] for r in rows} == {"r1", "r2"}


# ---------------------------------------------------------------------------
# Question handler — exactly-once answered / expired
# ---------------------------------------------------------------------------

async def test_question_request_and_answer_emit(mock_db):
    h = UserQuestionHandler()
    broadcast = AsyncMock(return_value=True)
    task = asyncio.create_task(h.request_answers(
        request_id="q1", tool_use_id="tu1", session_id="s1",
        questions=[{"question": "Pick one", "header": "h"}],
        broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "q1", kind="question")

    requested = await _wait_events("s1", "interaction.requested", 1)
    assert len(requested) == 1
    assert requested[0]["payload"]["kind"] == "question"
    assert requested[0]["payload"]["request_id"] == "q1"

    assert await h.respond("q1", "s1", {"h": "A"}) is True
    await asyncio.wait_for(task, 12.0)

    answered = _events_of_type("s1", "interaction.answered")
    assert len(answered) == 1
    assert answered[0]["payload"]["request_id"] == "q1"

    # Second answer: no pending row → guarded UPDATE never runs → nothing.
    assert await h.respond("q1", "s1", {"h": "B"}) is False
    assert len(_events_of_type("s1", "interaction.answered")) == 1


async def test_question_cancel_all_emits_expired(mock_db):
    h = UserQuestionHandler()
    broadcast = AsyncMock(return_value=True)
    task = asyncio.create_task(h.request_answers(
        request_id="q1", tool_use_id="tu1", session_id="s1",
        questions=[{"question": "Pick one", "header": "h"}],
        broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "q1", kind="question")

    await h.cancel_all("s1")
    await asyncio.wait_for(task, 12.0)

    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 1
    assert rows[0]["payload"]["reason"] == "cancelled"
    assert rows[0]["payload"]["request_id"] == "q1"


# ---------------------------------------------------------------------------
# Contract regen — the committed artifacts carry the new fields
# ---------------------------------------------------------------------------

def test_contract_artifacts_carry_new_fields():
    from pathlib import Path

    schema = Path(__file__).resolve().parents[2] / "app" / "contract" / "schema" / "events.schema.json"
    text = schema.read_text(encoding="utf-8")
    assert '"request_id"' in text
    assert '"reason"' in text
