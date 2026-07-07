"""
The three latent queue/preempt race gaps documented (as pre-existing, deferred)
in commit a361648f — closed here:

GAP 1 — duplicate user-message persist on a preempt race.
    Both ``handle_query`` and ``_process_next_queued`` used to persist the user
    message BEFORE the late check-and-register. On a preempt the prompt was
    enqueued / re-queued — and the queue drain later persisted it a SECOND time
    (duplicate ``session_messages`` row + duplicate chat bubble). Fix: the
    persist (+ ``broadcast_message_added``) is ordered AFTER winning the
    registration, so exactly ONE path — the one that actually starts the turn —
    persists the prompt.

GAP 2 — queue-order inversion when two drains race.
    Two concurrent ``_process_next_queued`` calls (e.g. a finishing turn's
    finally-fallback + a ``queue_message``/``load_session`` drain from another
    socket) each popped a DIFFERENT message; whichever registered second
    re-queued its message at the front — BEHIND a message queued after it, which
    was already running (inversion). Fix: a per-session ``_draining_sessions``
    reservation (the closure's mechanism, ported) taken under
    ``_active_sessions_lock`` BEFORE the pop — the losing drain bails without
    popping, so messages always start in queue order.

GAP 3 — the late check-and-register omitted the stop/interrupt latches.
    ``_process_next_queued``'s callers check ``_stopped_sessions`` /
    ``_interrupt_pending_sessions`` BEFORE calling, but the drain has awaits
    between its pop and its registration; a Stop / interrupt_for_query landing
    in that window was ignored and the queued message started a turn the user
    had just tried to stop. Fix: the latches are re-checked inside the SAME lock
    acquisition as the check-and-register; when latched the message is re-queued
    at the front (stop semantics: the queue stays intact) and the latch itself
    SURVIVES (never cleared by the drain).

Harness reuses tests/api/test_chat_ws_handler_queue.py's ``_Harness`` (fake WS,
mocked collaborators, in-memory queue table; no Postgres / SDK), plus a fresh
per-test ``_draining_sessions`` set.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import app.api.chat_ws_handler as cwh  # noqa: F401  (patch surface)
import app.api.websocket as ws_mod

from tests.api.test_chat_ws_handler_queue import _FakeLiveTask, _Harness, _queued


class _GapHarness(_Harness):
    """_Harness + an isolated per-test ``_draining_sessions`` reservation set."""

    def __enter__(self):
        super().__enter__()
        self.draining: set = set()
        self._stack.enter_context(
            patch.object(ws_mod, "_draining_sessions", new=self.draining)
        )
        return self


# ═══ GAP 1 — duplicate user-message persist on preempt ═══════════════════════


async def test_drain_preempt_does_not_double_persist_user_message():
    """A drain preempted at its late check-and-register (an interloper turn
    registered during the drain's awaits) re-queues the message WITHOUT having
    persisted it; the later drain that actually runs it persists it exactly
    once. Before the fix the message was persisted by BOTH drains (dup row)."""
    with _GapHarness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("hello race"))

        interloper = _FakeLiveTask()

        def _get_session_and_inject(sid):
            # The worktree lookup is the last await before the registration —
            # the interloper turn appears exactly in the preempt window.
            h.registry[sid] = interloper
            row = dict(h.session_row)
            row.setdefault("id", sid)
            return row

        h.db.get_session = MagicMock(side_effect=_get_session_and_inject)

        await handler._process_next_queued("sess-1")

        # Preempted: re-queued at the front, nothing persisted, no turn spawned.
        h.db.requeue_message_at_front.assert_called_once()
        assert [m["content"] for m in h.queue] == ["hello race"]
        h.db.add_session_message.assert_not_called(), (
            "a preempted drain must not persist the message it did not run"
        )
        handler.run_query.assert_not_called()

        # Interloper finishes; the next drain runs the message — persisting it
        # for the FIRST and ONLY time.
        del h.registry["sess-1"]
        h.db.get_session = MagicMock(
            side_effect=lambda sid: {**h.session_row, "id": sid}
        )
        await handler._process_next_queued("sess-1")
        await asyncio.sleep(0)

        assert h.db.add_session_message.call_count == 1, (
            "the user message must be persisted exactly once (the dup-persist gap)"
        )
        assert h.db.add_session_message.call_args.kwargs["content"] == "hello race"
        handler.run_query.assert_called_once()
        assert h.queue == []


async def test_handle_query_preempt_does_not_double_persist():
    """handle_query preempted at registration (a server-initiated turn won the
    race during the persist-window awaits) enqueues the prompt WITHOUT having
    persisted it; the eventual drain persists it exactly once."""
    with _GapHarness() as h:
        handler = h.make_handler()
        handler.run_query = AsyncMock(return_value=None)

        interloper = _FakeLiveTask()

        async def _inject(*args, **kwargs):
            h.registry["sess-1"] = interloper

        h.sync.register_device = AsyncMock(side_effect=_inject)

        await handler.handle_query({
            "prompt": "raced prompt",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

        h.db.add_queued_message.assert_called_once()
        assert h.db.add_queued_message.call_args.kwargs["content"] == "raced prompt"
        h.db.add_session_message.assert_not_called(), (
            "a preempted query must not persist the prompt it did not run"
        )
        handler.run_query.assert_not_called()

        # Interloper finishes; drain runs the enqueued prompt — one persist.
        del h.registry["sess-1"]
        h.sync.register_device = AsyncMock()
        await handler._process_next_queued("sess-1")
        await asyncio.sleep(0)

        assert h.db.add_session_message.call_count == 1, (
            "the user message must be persisted exactly once (the dup-persist gap)"
        )
        assert h.db.add_session_message.call_args.kwargs["content"] == "raced prompt"


# ═══ GAP 2 — queue-order inversion on racing drains ══════════════════════════


async def test_racing_drains_do_not_invert_queue_order():
    """Two drains race: drain1 pops the FIRST message and is suspended in its
    post-pop awaits; drain2 arrives. Before the fix drain2 popped the SECOND
    message and ran it while drain1 got preempted and re-queued the FIRST at
    the front — execution order inverted. With the ``_draining_sessions``
    reservation drain2 bails before popping; drain1 runs the first message and
    the second stays queued behind it."""
    with _GapHarness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        prompts: list = []
        run_gate = asyncio.Event()

        async def _gated_run_query(prompt, *a, **k):
            prompts.append(prompt)
            await run_gate.wait()

        handler.run_query = _gated_run_query

        h.queue.append(_queued("first"))
        h.queue.append({"id": "q2", "content": "second",
                        "profile_id": None, "project_id": None})

        drain1_parked = asyncio.Event()
        drain1_resume = asyncio.Event()
        broadcast_calls = {"n": 0}

        async def _park_first_broadcast(*a, **k):
            broadcast_calls["n"] += 1
            if broadcast_calls["n"] == 1:
                drain1_parked.set()
                await drain1_resume.wait()

        h.sync.broadcast_queue_updated = AsyncMock(side_effect=_park_first_broadcast)

        drain1 = asyncio.create_task(handler._process_next_queued("sess-1"))
        await asyncio.wait_for(drain1_parked.wait(), timeout=5.0)

        # drain2 races in while drain1 sits between its pop and its register.
        await handler._process_next_queued("sess-1")

        drain1_resume.set()
        await asyncio.wait_for(drain1, timeout=5.0)
        await asyncio.sleep(0)  # let the spawned turn record its prompt

        try:
            # Queue order preserved: "first" runs, "second" waits behind it.
            assert prompts == ["first"], "the FIRST queued message must run first"
            assert [m["content"] for m in h.popped] == ["first"], (
                "the racing drain must not pop a second message"
            )
            assert [m["content"] for m in h.queue] == ["second"]
            h.db.requeue_message_at_front.assert_not_called()
            assert h.draining == set(), "reservation must be released"
        finally:
            run_gate.set()
            task = h.registry.get("sess-1")
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)


async def test_sequential_drains_run_in_queue_order_and_release_reservation():
    """Regression guard for the reservation: back-to-back (non-racing) drains
    still run both messages, in order, and never leak the reservation."""
    with _GapHarness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("first"))
        h.queue.append({"id": "q2", "content": "second",
                        "profile_id": None, "project_id": None})

        await handler._process_next_queued("sess-1")
        await asyncio.sleep(0)  # first spawned (stub) turn retires
        await handler._process_next_queued("sess-1")
        await asyncio.sleep(0)

        assert [c.args[0] for c in handler.run_query.call_args_list] == [
            "first", "second",
        ]
        assert h.db.add_session_message.call_count == 2
        assert h.queue == []
        assert h.draining == set()


# ═══ GAP 3 — stop/interrupt latch landing inside the drain window ════════════


async def test_stop_latched_mid_drain_aborts_and_requeues():
    """A Stop that lands between the drain's pop and its registration must NOT
    be lost: the drain aborts under the same lock as the check-and-register,
    re-queues the message at the front (queue stays intact — stop semantics),
    persists nothing, and leaves the latch set (a Stop is never un-done by a
    drain)."""
    with _GapHarness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("stopped mid-drain"))

        def _get_session_and_latch(sid):
            h.stopped.add(sid)
            row = dict(h.session_row)
            row.setdefault("id", sid)
            return row

        h.db.get_session = MagicMock(side_effect=_get_session_and_latch)

        await handler._process_next_queued("sess-1")

        handler.run_query.assert_not_called(), (
            "a stop arriving mid-drain must not be lost (no turn may start)"
        )
        assert "sess-1" not in h.registry
        h.db.requeue_message_at_front.assert_called_once()
        assert [m["content"] for m in h.queue] == ["stopped mid-drain"]
        assert "sess-1" in h.stopped, "the drain must never clear a user Stop"
        h.db.add_session_message.assert_not_called()
        assert h.draining == set()


async def test_interrupt_latched_mid_drain_aborts_and_requeues():
    """Same window as the Stop case, for a two-phase interrupt_for_query: the
    interrupt owns the next turn (the frontend sends the real query after the
    confirm) — the drain must yield, re-queue, and leave the latch alone."""
    with _GapHarness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("interrupted mid-drain"))

        def _get_session_and_latch(sid):
            h.interrupt_pending.add(sid)
            row = dict(h.session_row)
            row.setdefault("id", sid)
            return row

        h.db.get_session = MagicMock(side_effect=_get_session_and_latch)

        await handler._process_next_queued("sess-1")

        handler.run_query.assert_not_called()
        assert "sess-1" not in h.registry
        h.db.requeue_message_at_front.assert_called_once()
        assert [m["content"] for m in h.queue] == ["interrupted mid-drain"]
        assert "sess-1" in h.interrupt_pending
        h.db.add_session_message.assert_not_called()
        assert h.draining == set()
