"""Q4 — permissions & questions on shared ``pending_interactions`` (V2 Phase 4).

The permission-request and AskUserQuestion flows persist as durable
``pending_interactions`` rows so ANY install answers them relay-free; the
executing runner blocks on the row via
``interaction_wait.await_interaction``. These tests exercise the adapter seams on
the real pg-shim SQLite DB (``mock_db``):

* permission create/allow/deny/cancel/expire round-trips + ``answered_by_device``
  stamping (Q4-6);
* the defer -> requeue mapping (Q4-2): defer ANSWERS the row (waiter gets a
  ``_DeferSentinel``); requeue mints a FRESH pending row carrying the same FE
  request_id and drops the old answered-defer row from the derived deferred set;
* question create/answer/cancel round-trips;
* the dedicated expiry sweeper (Q4-4) start/stop + expire-and-notify pass;
* the ``await_interaction`` local-deadline fallback (belt to the sweeper) and its
  same-process wake.

The gateway LISTEN thread and mobile push are patched off (autouse) so the suite
never spins a real listener or notifier — correctness rides the in-process wake +
poll cadence, exactly as documented in ``interaction_wait``.
"""

import asyncio

from unittest.mock import AsyncMock

from app.core.agent_types import PermissionResultAllow, PermissionResultDeny

from app.core import interaction_sweeper, interaction_wait
from app.core.permission_handler import PermissionHandler, _DeferSentinel
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

async def _wait_pending(session_id, request_id, kind="permission", timeout=2.0):
    """Poll the shared table until request_permission_v2 has persisted its row."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for r in interactions.get_pending_interactions(session_id):
            if r.get("kind") == kind and (r.get("payload") or {}).get("request_id") == request_id:
                return r
        await asyncio.sleep(0.01)
    raise AssertionError(f"pending {kind} row for {request_id} never appeared")


def _answered_row(session_id, request_id, kind="permission"):
    for r in interactions.get_answered_interactions(session_id, kind):
        if (r.get("payload") or {}).get("request_id") == request_id:
            return r
    return None


def _events_of_type(session_id, event_type):
    return [
        e for e in session_events_db.get_events(session_id)
        if e.get("event_type") == event_type
    ]


# ---------------------------------------------------------------------------
# Permission — allow / deny / device stamping
# ---------------------------------------------------------------------------

async def test_v2_permission_allow_wakes_waiter_and_stamps_device(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    # A PEER install answers directly against the table.
    result = await h.respond(
        "r1", "s1", "allow", broadcast_func=broadcast, answered_by_device="dev-2"
    )
    assert result["resolved"] is True

    out = await asyncio.wait_for(task, 12.0)
    assert isinstance(out, PermissionResultAllow)
    assert out.updated_input == {"command": "ls"}

    row = _answered_row("s1", "r1")
    assert row is not None
    assert row["answered_by_device"] == "dev-2"  # Q4-6
    assert row["answer"]["decision"] == "allow"
    # A permission_request event went out with the byte-identical legacy shape.
    types = [c.args[0].get("type") for c in broadcast.call_args_list if c.args]
    assert "permission_request" in types


async def test_v2_permission_deny_denies_waiter(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "rm"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    await h.respond("r1", "s1", "deny", broadcast_func=broadcast)
    out = await asyncio.wait_for(task, 12.0)
    assert isinstance(out, PermissionResultDeny)


async def test_v2_permission_respond_unknown_request(mock_db):
    h = PermissionHandler()
    result = await h.respond("nope", "s1", "allow", broadcast_func=AsyncMock())
    assert result == {"error": "Request not found"}


# ---------------------------------------------------------------------------
# Permission — defer -> requeue mapping (Q4-2)
# ---------------------------------------------------------------------------

async def test_v2_permission_defer_returns_sentinel_then_requeue_fresh_row(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    res = await h.respond("r1", "s1", "defer", broadcast_func=broadcast)
    assert res["deferred"] is True

    out = await asyncio.wait_for(task, 12.0)
    assert isinstance(out, _DeferSentinel)

    # Derived deferred set surfaces it; it is no longer pending.
    assert [d["request_id"] for d in h.get_deferred_requests("s1")] == ["r1"]
    assert h.get_pending_requests("s1") == []

    # Requeue mints a FRESH pending row (same request_id, fresh TTL) and drops
    # the old answered-defer row from the deferred set.
    requeued = await h.requeue_deferred("s1", broadcast_func=broadcast)
    assert requeued == ["r1"]
    assert [p["request_id"] for p in h.get_pending_requests("s1")] == ["r1"]
    assert h.get_deferred_requests("s1") == []


# ---------------------------------------------------------------------------
# Permission — remember creates a rule + auto-resolves matching pendings
# ---------------------------------------------------------------------------

async def test_v2_permission_remember_profile_auto_resolves_matching(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    t1 = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")
    t2 = asyncio.create_task(h.request_permission(
        request_id="r2", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "pwd"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r2")

    # Allow-remember-profile on r1 with a Bash:* pattern auto-resolves r2 too.
    result = await h.respond(
        "r1", "s1", "allow", remember="profile", pattern="*",
        broadcast_func=broadcast, answered_by_device="dev-1",
    )
    assert result["auto_resolved_ids"] == ["r2"]

    o1 = await asyncio.wait_for(t1, 12.0)
    o2 = await asyncio.wait_for(t2, 12.0)
    assert isinstance(o1, PermissionResultAllow)
    assert isinstance(o2, PermissionResultAllow)
    assert h.get_pending_requests("s1") == []


# ---------------------------------------------------------------------------
# A15 — the TOCTOU-losing responder honours answer_interaction's False return.
# ---------------------------------------------------------------------------

async def test_v2_permission_respond_toctou_loser_no_side_effects(mock_db, monkeypatch):
    """A concurrent responder answered the row first; the loser reaches
    answer_interaction (row still looked pending) and it returns False. The loser
    must report not-resolved and leave NO side effects: no session rule, no
    requeue row, and it must not overwrite the winner's stamp."""
    h = PermissionHandler()
    row = interactions.create_interaction(
        "s1",
        "permission",
        {
            "request_id": "r1",
            "profile_id": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        60,
    )
    # Winner answers the row first (a concurrent responder / peer install).
    assert interactions.answer_interaction(
        row["id"], {"decision": "allow", "behavior": "allow"}, "dev-win"
    ) is True

    # Loser still sees a stale "pending" snapshot, so it proceeds to the guarded
    # UPDATE — which now returns False against the real answer_interaction.
    monkeypatch.setattr(
        h, "_v2_pending_permissions", lambda sid: [interactions.get_interaction(row["id"])]
    )
    result = await h.respond(
        "r1", "s1", "allow", remember="session", pattern="*",
        broadcast_func=AsyncMock(), answered_by_device="dev-lose",
    )
    assert result == {"error": "Request not found"}

    # No session rule persisted by the loser.
    assert h.get_session_rules("s1") == []
    # No requeue / new pending row minted; the winner's answer is untouched.
    assert interactions.get_pending_interactions("s1") == []
    answered = interactions.get_answered_interactions("s1", "permission")
    assert len(answered) == 1
    assert answered[0]["answered_by_device"] == "dev-win"
    assert "remember" not in answered[0]["answer"]


async def test_v2_permission_defer_toctou_loser_no_side_effects(mock_db, monkeypatch):
    """A15 for the defer arm: a concurrent responder already answered the row;
    the defer loser reaches answer_interaction (row still looked pending) and it
    returns False. The loser must report not-resolved and leave NO side effects:
    no broadcast, and no fresh requeue/pending row."""
    h = PermissionHandler()
    row = interactions.create_interaction(
        "s1",
        "permission",
        {
            "request_id": "r1",
            "profile_id": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        60,
    )
    # Winner answers the row first (a concurrent responder / peer install).
    assert interactions.answer_interaction(
        row["id"], {"decision": "allow", "behavior": "allow"}, "dev-win"
    ) is True

    # Loser still sees a stale "pending" snapshot, so it proceeds to the guarded
    # defer UPDATE — which now returns False against the real answer_interaction.
    monkeypatch.setattr(
        h, "_v2_pending_permissions", lambda sid: [interactions.get_interaction(row["id"])]
    )
    broadcast = AsyncMock()
    result = await h.respond(
        "r1", "s1", "defer", broadcast_func=broadcast, answered_by_device="dev-lose",
    )
    assert result == {"error": "Request not found"}

    # No broadcast fired, no requeue / new pending row minted; the winner's
    # answer is untouched.
    broadcast.assert_not_called()
    assert interactions.get_pending_interactions("s1") == []
    answered = interactions.get_answered_interactions("s1", "permission")
    assert len(answered) == 1
    assert answered[0]["answered_by_device"] == "dev-win"
    assert answered[0]["answer"]["decision"] == "allow"


# ---------------------------------------------------------------------------
# A14 — cross-install remember:"session" propagation (the F5 fix).
# ---------------------------------------------------------------------------

async def test_v2_permission_session_remember_propagates_cross_install(mock_db):
    """Install B answers with remember="session"; the executing runner (install A,
    a distinct handler instance sharing only the table) must learn the rule from
    the shared answer JSONB and short-circuit an identical follow-up request
    WITHOUT minting a new pending row."""
    hA = PermissionHandler()  # executing runner (blocks on the row)
    hB = PermissionHandler()  # answering install
    broadcast = AsyncMock()

    task = asyncio.create_task(hA.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")
    assert hA.get_session_rules("s1") == []  # A never saw the rule locally

    res = await hB.respond(
        "r1", "s1", "allow", remember="session", pattern="*",
        broadcast_func=broadcast, answered_by_device="dev-B",
    )
    assert res["resolved"] is True

    out = await asyncio.wait_for(task, 12.0)
    assert isinstance(out, PermissionResultAllow)

    # The hint rides the shared row...
    row = _answered_row("s1", "r1")
    assert row["answer"]["remember"] == "session"
    assert row["answer"]["pattern"] == "*"
    # ...and the executing runner installed the identical local rule.
    rules = hA.get_session_rules("s1")
    assert len(rules) == 1
    assert rules[0]["tool_name"] == "Bash"
    assert rules[0]["tool_pattern"] == "*"
    assert rules[0]["decision"] == "allow"

    # A second identical request short-circuits with NO new pending row.
    out2 = await asyncio.wait_for(hA.request_permission(
        request_id="r2", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls -la"}, broadcast_func=broadcast,
    ), 12.0)
    assert isinstance(out2, PermissionResultAllow)
    assert not any(
        (r.get("payload") or {}).get("request_id") == "r2"
        for r in interactions.get_pending_interactions("s1")
    )


async def test_v2_resolve_matching_stamps_session_remember(mock_db):
    """Rows batch-resolved via _v2_resolve_matching under a session-remember also
    carry the stamp, so the executing runner installs the rule (idempotently)
    when each of their waiters wakes."""
    hA = PermissionHandler()  # executing runner
    hB = PermissionHandler()  # answering install
    broadcast = AsyncMock()

    t1 = asyncio.create_task(hA.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")
    t2 = asyncio.create_task(hA.request_permission(
        request_id="r2", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={"command": "pwd"}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r2")

    result = await hB.respond(
        "r1", "s1", "allow", remember="session", pattern="*",
        broadcast_func=broadcast, answered_by_device="dev-B",
    )
    assert result["auto_resolved_ids"] == ["r2"]

    await asyncio.wait_for(t1, 12.0)
    await asyncio.wait_for(t2, 12.0)

    # Both the directly-answered and the batch-resolved rows carry the stamp.
    assert _answered_row("s1", "r1")["answer"]["remember"] == "session"
    r2_row = _answered_row("s1", "r2")
    assert r2_row["answer"]["remember"] == "session"
    assert r2_row["answer"]["pattern"] == "*"
    # Two waiters woke and both replayed the hint — but the install is idempotent.
    assert len(hA.get_session_rules("s1")) == 1


# ---------------------------------------------------------------------------
# Permission — cancel / cancel_all / expiry timeout arm
# ---------------------------------------------------------------------------

async def test_v2_permission_cancel_request(mock_db):
    h = PermissionHandler()
    broadcast = AsyncMock()
    task = asyncio.create_task(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={}, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "r1")

    assert await h.cancel_request("r1", "s1") is True
    out = await asyncio.wait_for(task, 12.0)
    assert isinstance(out, PermissionResultDeny)
    assert await h.cancel_request("r1", "s1") is False  # already gone


async def test_v2_permission_cancel_all(mock_db):
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
        out = await asyncio.wait_for(t, 12.0)
        assert isinstance(out, PermissionResultDeny)
    assert h.get_queue_count("s1") == 0


async def test_v2_permission_local_deadline_timeout_denies(mock_db, monkeypatch):
    """When the local deadline lapses with the row still pending, the waiter
    denies and dismisses the modal (belt for a dead sweeper, Q4-4)."""
    import app.core.permission_handler as ph

    monkeypatch.setattr(ph, "PERMISSION_TTL_SECONDS", 0)
    h = PermissionHandler()
    broadcast = AsyncMock()

    out = await asyncio.wait_for(h.request_permission(
        request_id="r1", session_id="s1", profile_id="default",
        tool_name="Bash", tool_input={}, broadcast_func=broadcast,
    ), 3.0)
    assert isinstance(out, PermissionResultDeny)
    types = [c.args[0].get("type") for c in broadcast.call_args_list if c.args]
    assert "permission_timeout" in types


# ---------------------------------------------------------------------------
# Question — answer / device stamping / cancel
# ---------------------------------------------------------------------------

async def test_v2_question_answered_wakes_waiter_and_stamps_device(mock_db):
    h = UserQuestionHandler()
    broadcast = AsyncMock(return_value=True)
    questions = [{"question": "Pick one", "header": "H", "options": ["a", "b"]}]
    task = asyncio.create_task(h.request_answers(
        request_id="q1", tool_use_id="tu1", session_id="s1",
        questions=questions, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "q1", kind="question")

    ok = await h.respond("q1", "s1", {"H": "a"}, answered_by_device="dev-9")
    assert ok is True

    answers = await asyncio.wait_for(task, 12.0)
    assert answers == {"H": "a"}
    row = _answered_row("s1", "q1", kind="question")
    assert row["answered_by_device"] == "dev-9"


async def test_v2_question_get_pending_and_cancel(mock_db):
    h = UserQuestionHandler()
    broadcast = AsyncMock(return_value=True)
    questions = [{"question": "Q?", "header": "H"}]
    task = asyncio.create_task(h.request_answers(
        request_id="q1", tool_use_id="tu1", session_id="s1",
        questions=questions, broadcast_func=broadcast,
    ))
    await _wait_pending("s1", "q1", kind="question")

    pend = h.get_pending_questions("s1")
    assert [p["request_id"] for p in pend] == ["q1"]
    assert pend[0]["tool_use_id"] == "tu1"
    assert pend[0]["questions"] == questions

    await h.cancel_all("s1")
    answers = await asyncio.wait_for(task, 12.0)
    assert answers == {"error": "No answers received"}


async def test_v2_question_respond_unknown_returns_false(mock_db):
    h = UserQuestionHandler()
    assert await h.respond("nope", "s1", {"H": "a"}) is False


# ---------------------------------------------------------------------------
# Sweeper (Q4-4) — expire-and-notify pass, start/stop.
# ---------------------------------------------------------------------------

async def test_sweep_once_expires_overdue_and_notifies(mock_db, monkeypatch):
    interactions.create_interaction("s1", "permission", {}, 60)   # live
    interactions.create_interaction("s1", "permission", {}, -1)   # overdue
    interactions.create_interaction("s2", "question", {}, -1)     # overdue

    notified = []
    monkeypatch.setattr(
        interaction_wait, "notify_changed", lambda sid: notified.append(sid)
    )

    count = await interaction_sweeper.sweep_once()
    assert count == 2
    assert set(notified) == {"s1", "s2"}
    # Live row untouched; overdue rows flipped to expired.
    assert len(interactions.get_pending_interactions("s1")) == 1


async def test_sweeper_start_stop(mock_db):
    """start() registers the expiry pass on the shared sweep driver (F5).

    The sweeper no longer owns a private asyncio task: it contributes a pass to
    the one background driver in ``turn_claim``, so its lifecycle is
    register/unregister and the driver task is the thing that runs.
    """
    from app.core import turn_claim

    assert "interaction_expiry" not in turn_claim._passes
    try:
        interaction_sweeper.start()
        assert "interaction_expiry" in turn_claim._passes
        sweep = turn_claim._passes["interaction_expiry"]
        assert sweep.tight_interval == interaction_sweeper.SWEEP_INTERVAL_SECONDS
        assert sweep.run is interaction_sweeper.sweep_once
        # The driver came up on demand.
        driver = turn_claim._loop_task
        assert driver is not None and not driver.done()

        # Idempotent: a second start does not duplicate the pass or the driver.
        interaction_sweeper.start()
        assert turn_claim._loop_task is driver
        assert len([n for n in turn_claim._passes if n == "interaction_expiry"]) == 1

        await interaction_sweeper.stop()
        assert "interaction_expiry" not in turn_claim._passes
    finally:
        await turn_claim.stop()


# ---------------------------------------------------------------------------
# Sweeper (PR-B) — durable interaction.expired (reason=ttl) emission.
# ---------------------------------------------------------------------------

async def test_sweep_once_emits_expired_ttl_with_request_id(mock_db):
    """A swept overdue row emits exactly one interaction.expired (reason=ttl)
    carrying the row's request_id (nested in payload)."""
    interactions.create_interaction(
        "s1", "permission", {"request_id": "req-x"}, -1,   # overdue
    )

    count = await interaction_sweeper.sweep_once()
    assert count == 1

    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 1
    assert rows[0]["payload"]["reason"] == "ttl"
    assert rows[0]["payload"]["request_id"] == "req-x"
    # Unfenced append: the sweeper holds no run lease.
    assert rows[0]["run_id"] is None


async def test_sweep_once_answered_row_emits_nothing(mock_db):
    """A row that lost the guarded UPDATE race (already answered, not pending)
    is never swept, so the sweeper emits no interaction.expired for it —
    preserving the exactly-once-terminal-event invariant."""
    answered = interactions.create_interaction(
        "s1", "permission", {"request_id": "req-answered"}, 60,
    )
    assert interactions.answer_interaction(answered["id"], {"decision": "allow"}) is True

    overdue = interactions.create_interaction(
        "s1", "permission", {"request_id": "req-overdue"}, -1,
    )

    count = await interaction_sweeper.sweep_once()
    assert count == 1  # only the overdue-pending row is swept

    rows = _events_of_type("s1", "interaction.expired")
    assert len(rows) == 1
    assert {r["payload"]["request_id"] for r in rows} == {"req-overdue"}
    # The already-answered row produced no expired event.
    assert overdue["id"] != answered["id"]


async def test_sweep_once_emission_failure_does_not_break_sweep(mock_db, monkeypatch):
    """An append failure inside emission is swallowed and never wedges the
    sweep: the rows are still flipped to expired and the count is returned."""
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_events_db, "append_events", _boom)

    overdue = interactions.create_interaction(
        "s1", "permission", {"request_id": "req-x"}, -1,
    )

    # Must not raise despite the failing append.
    count = await interaction_sweeper.sweep_once()
    assert count == 1
    # The row was still expired (the DB sweep is independent of emission).
    assert interactions.get_interaction(overdue["id"])["state"] == "expired"
    # And nothing was recorded.
    assert _events_of_type("s1", "interaction.expired") == []


# ---------------------------------------------------------------------------
# await_interaction — local-deadline fallback + same-process wake.
# ---------------------------------------------------------------------------

async def test_await_interaction_local_deadline_returns_pending(mock_db):
    row = interactions.create_interaction("s1", "permission", {}, 60)
    got = await asyncio.wait_for(
        interaction_wait.await_interaction(row["id"], "s1", 0.05, poll_seconds=0.02),
        2.0,
    )
    assert got is not None and got["state"] == "pending"


async def test_await_interaction_wakes_on_notify(mock_db):
    row = interactions.create_interaction("s1", "permission", {}, 60)

    async def _answer_soon():
        await asyncio.sleep(0.05)
        interactions.answer_interaction(row["id"], {"decision": "allow"}, "dev-1")
        interaction_wait.notify_changed("s1")

    asyncio.create_task(_answer_soon())
    got = await asyncio.wait_for(
        # Long poll cadence: only the notify wake can return this promptly.
        interaction_wait.await_interaction(row["id"], "s1", 30.0, poll_seconds=25.0),
        2.0,
    )
    assert got["state"] == "answered"
    assert got["answer"]["decision"] == "allow"


async def test_await_interaction_vanished_returns_none(mock_db):
    got = await asyncio.wait_for(
        interaction_wait.await_interaction("ghost", "s1", 1.0, poll_seconds=0.02),
        2.0,
    )
    assert got is None


# ---------------------------------------------------------------------------
# A16 — notify_changed must not block the loop; the DB leg rides a task and any
# NOTIFY error is swallowed.
# ---------------------------------------------------------------------------

async def test_notify_changed_schedules_db_leg_off_loop_and_swallows(mock_db, monkeypatch):
    import app.db.session_events as se

    calls = []

    def _boom(session_id):
        calls.append(session_id)
        raise RuntimeError("notify_session blew up")

    monkeypatch.setattr(se, "notify_session", _boom)

    # In-proc leg still fires synchronously; DB leg is deferred to a task.
    ticks = []
    monkeypatch.setattr(interaction_wait, "_on_gateway_tick", lambda sid: ticks.append(sid))

    interaction_wait.notify_changed("s1")  # must not raise

    assert ticks == ["s1"]          # same-process wake ran synchronously
    assert calls == []              # DB NOTIFY did NOT run on the event loop

    # Let the fire-and-forget task drain (it hops to a worker thread, so a
    # fixed sleep raced it on a loaded machine).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while not calls and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert calls == ["s1"]          # DB leg ran off-loop; its exception swallowed
