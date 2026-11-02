"""A replaced engine's late writes cannot corrupt its successor (#1027).

Cancelling a task does not stop the thread running its transaction, so a write
the old engine already started can commit after a new engine has taken over.
These cases hold that write at a chosen point on real PostgreSQL, so the
crossing is deterministic rather than a matter of timing:

* ``before``: the write has not started its transaction when the takeover runs;
* ``commit``: the transaction has done its work and is about to commit.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from tests.db.postgres_harness import (  # noqa: F401  (pg_schema: autouse fixture)
    pg_schema,
    requires_real_pg,
)

pytestmark = requires_real_pg


@dataclass
class Hold:
    """Where one write stops, and the two signals that drive it."""

    point: str
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def stop(self, point: str) -> None:
        if point == self.point:
            self.reached.set()
            assert self.release.wait(30), "the test never released this write"


_HOLD: contextvars.ContextVar[Hold | None] = contextvars.ContextVar("hold", default=None)


@pytest.fixture(autouse=True)
def _holdable_writes(monkeypatch):
    """Let one write stop before its transaction, or just before its commit."""
    from app.core.provider import fluid_pg_store as module

    def holdable(real):
        @contextmanager
        def opened(*args, **kwargs):
            hold = _HOLD.get()
            try:
                if hold:
                    hold.stop("before")
                with real(*args, **kwargs) as conn:
                    yield conn
                    if hold:
                        hold.stop("commit")
            finally:
                if hold:
                    hold.finished.set()
        return opened

    for name in ("get_db", "get_turn_db", "get_event_db"):
        monkeypatch.setattr(module, name, holdable(getattr(module, name)))


async def _held(coro, point: str) -> tuple[asyncio.Task, Hold]:
    """Start ``coro`` and return once its write has reached ``point``."""
    hold = Hold(point)
    token = _HOLD.set(hold)
    try:
        task = asyncio.ensure_future(coro)
    finally:
        _HOLD.reset(token)
    while not hold.reached.is_set():
        await asyncio.sleep(0.005)
    return task, hold


async def _take_over(store, session_id: str) -> None:
    """What ``HarnessAdapter.connect`` does before anything reads the session."""
    claim = getattr(store, "claim_session", None)
    if callable(claim):
        await claim(session_id)


async def _session() -> tuple[object, str]:
    """A session with one saved event, owned by the store returned."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    store = PgFluidStore()
    meta = await store.create_session(f"ses_{uuid4().hex[:10]}")
    await _take_over(store, meta.session_id)
    await store.get_session(meta.session_id)
    await store.append_event(meta.session_id, _event(meta.session_id, 1, "first"))
    return store, meta.session_id


def _event(session_id: str, seq: int, message: str):
    from fluid.events.model import Interrupted

    return Interrupted(session_id=session_id, seq=seq, message=message)


def _node(node_id: str, parent_id):
    from fluid.messages import Message
    from fluid.messages.content import Text
    from fluid.store.base import SessionNode

    return SessionNode(
        node_id=node_id, parent_id=parent_id,
        message=Message(role="user", content=[Text(node_id)]),
    )


def _rows(sql: str, params: tuple) -> list[dict]:
    from app.db import base

    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _events(session_id: str) -> dict[int, str]:
    return {
        row["seq"]: row["event"]["message"]
        for row in _rows(
            "SELECT seq, event FROM fluid_session_events WHERE session_id = %s ORDER BY seq",
            (session_id,),
        )
    }


def _session_row(session_id: str) -> dict:
    return _rows("SELECT * FROM fluid_sessions WHERE session_id = %s", (session_id,))[0]


# ─── The three failures ──────────────────────────────────────────────────────

async def test_late_event_does_not_take_the_number_the_new_engine_was_given():
    """Lost event: the new engine seeds from committed rows, then the old commits."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    late, hold = await _held(old.append_event(sid, _event(sid, 2, "old")), "before")

    new = PgFluidStore()
    await _take_over(new, sid)
    seeded = await new.last_event_seq(sid)
    assert seeded == 1

    hold.release.set()
    await late  # Rejected without an error: the old engine's turn is not failed.

    await new.append_event(sid, _event(sid, seeded + 1, "new"))
    assert _events(sid) == {1: "first", 2: "new"}


async def test_takeover_waits_for_a_write_already_committing():
    """An open transaction is ordered ahead of the takeover, so its number is seen."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    late, hold = await _held(old.append_event(sid, _event(sid, 2, "old")), "commit")

    new = PgFluidStore()

    async def seed() -> int:
        await _take_over(new, sid)
        return await new.last_event_seq(sid)

    seeding = asyncio.ensure_future(seed())
    await asyncio.sleep(0.3)
    hold.release.set()
    await late
    seeded = await seeding

    await new.append_event(sid, _event(sid, seeded + 1, "new"))
    assert _events(sid) == {1: "first", 2: "old", 3: "new"}


async def test_late_node_does_not_move_the_head_back():
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    late, hold = await _held(old.append(sid, _node("old-node", "n1")), "before")

    new = PgFluidStore()
    await _take_over(new, sid)
    meta = await new.get_session(sid)
    assert meta.head_node_id == "n1"
    await new.append(sid, _node("new-node", "n1"))

    hold.release.set()
    await late

    assert _session_row(sid)["head_node_id"] == "new-node"
    nodes = _rows("SELECT node_id FROM fluid_session_nodes WHERE session_id = %s", (sid,))
    assert {row["node_id"] for row in nodes} == {"n1", "new-node"}


async def test_late_handle_erase_does_not_touch_the_new_engines_handle():
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.set_cli_session_id(sid, "cli-old")
    late, hold = await _held(old.set_cli_session_id(sid, None), "before")

    new = PgFluidStore()
    await _take_over(new, sid)
    await new.get_session(sid)
    await new.set_cli_session_id(sid, "cli-new")

    hold.release.set()
    await late

    assert _session_row(sid)["cli_session_id"] == "cli-new"


# ─── Writes queued behind a takeover ─────────────────────────────────────────

@pytest.mark.parametrize("write", ["event", "node", "head", "handle"])
async def test_write_waiting_on_a_takeover_is_refused_when_it_commits(write):
    """A write blocked by the takeover's row lock re-reads the row, then stops."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    await old.set_cli_session_id(sid, "cli-old")
    new = PgFluidStore()

    claiming, hold = await _held(new.claim_session(sid), "commit")
    late = asyncio.ensure_future({
        "event": lambda: old.append_event(sid, _event(sid, 2, "old")),
        "node": lambda: old.append(sid, _node("old-node", "n1")),
        "head": lambda: old.set_head(sid, None),
        "handle": lambda: old.set_cli_session_id(sid, None),
    }[write]())
    # Queued on the row lock: its statement is running, not finished.
    await asyncio.sleep(0.3)
    assert not late.done()

    hold.release.set()
    await claiming
    await late

    row = _session_row(sid)
    assert old.lost_ownership() is True
    assert _events(sid) == {1: "first"}
    assert row["head_node_id"] == "n1"
    assert row["cli_session_id"] == "cli-old"


# ─── What the old engine sees ────────────────────────────────────────────────

async def test_refused_writes_raise_nothing_and_keep_the_old_turn_coherent():
    """The old turn may still be streaming: no error, and its own cache advances."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    new = PgFluidStore()
    await _take_over(new, sid)

    await old.append_event(sid, _event(sid, 2, "refused"))
    await old.append(sid, _node("old-node", "n1"))
    # The old turn builds its next prompt from what it just appended.
    assert (await old.get_node(sid, "old-node")).node_id == "old-node"
    assert (await old.get_session(sid)).head_node_id == "old-node"
    await old.set_head(sid, "old-node")
    await old.set_cli_session_id(sid, None)
    await old.flush_events(sid)

    assert old.lost_ownership() is True
    assert (await old.get_session(sid)).head_node_id == "old-node"
    assert _events(sid) == {1: "first"}
    assert _session_row(sid)["head_node_id"] == "n1"
    assert new.lost_ownership() is False


async def test_refused_stream_batch_is_forgotten_rather_than_retried():
    from fluid.events.model import TextDelta

    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.append_stream_event(sid, TextDelta(session_id=sid, seq=2, index=0, text="x"))
    assert old._pending_events[sid]

    await _take_over(PgFluidStore(), sid)
    await old.flush_events(sid)

    assert not old._pending_events.get(sid)
    assert _events(sid) == {1: "first"}


async def _turn_crossing_a_takeover(old_loop, old_store, new_turn, sid):
    """Stop the old engine mid-reply, run the new engine's turn, then let it go."""
    from fluid.events import UserTurn
    from fluid.events.model import ErrorEvent, TurnComplete

    stopped, resume = asyncio.Event(), asyncio.Event()
    accept_delta = old_store.append_stream_event
    accepted = 0

    async def append_stream_event(session_id, event):
        nonlocal accepted
        await accept_delta(session_id, event)
        accepted += 1
        if accepted == 5:
            stopped.set()
            await resume.wait()

    old_store.append_stream_event = append_stream_event
    old_store.STREAM_BATCH_LIMIT = 2  # Some of the reply is durable before the crossing.
    await old_loop.submit(UserTurn(content="old turn"))
    await asyncio.wait_for(stopped.wait(), 30)

    await new_turn()
    before = _events_raw(sid)
    head = _session_row(sid)["head_node_id"]

    resume.set()
    await asyncio.wait_for(old_loop.wait_turn(), 60)
    published = old_loop.bus.log(sid)

    # The old engine finished its reply normally and reported no failure.
    assert any(isinstance(event, TurnComplete) for event in published)
    assert not any(isinstance(event, ErrorEvent) for event in published)
    # Nothing it wrote afterwards reached the session.
    assert _events_raw(sid) == before
    assert _session_row(sid)["head_node_id"] == head
    assert old_store.lost_ownership() is True
    return before


def _events_raw(session_id: str) -> list[tuple[int, str]]:
    return [
        (row["seq"], row["event"].get("_cls"))
        for row in _rows(
            "SELECT seq, event FROM fluid_session_events WHERE session_id = %s ORDER BY seq",
            (session_id,),
        )
    ]


def _assert_new_turn_complete(events: list[tuple[int, str]]) -> None:
    """Numbers carry on unbroken, and the new engine's own turn is all there."""
    numbers = [seq for seq, _ in events]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    assert [cls for _, cls in events].count("TurnComplete") == 1


async def test_real_native_engine_crossing_a_takeover():
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn

    from app.core.provider.fluid_pg_store import PgFluidStore
    from tests.db.native_crash_producer import scripted_provider

    def loop(store, session_id):
        return NativeLoop(
            provider=scripted_provider(12), store=store,
            model="scripted-native", session_id=session_id,
        )

    old_store = PgFluidStore()
    sid = f"own-native-{uuid4().hex[:8]}"
    old = loop(old_store, sid)
    new_store = PgFluidStore()
    new = None

    async def new_turn():
        nonlocal new
        await new_store.claim_session(sid)
        new = loop(new_store, sid)
        await new.submit(UserTurn(content="new turn"))
        await asyncio.wait_for(new.wait_turn(), 60)

    try:
        events = await _turn_crossing_a_takeover(old, old_store, new_turn, sid)
    finally:
        await old.aclose()
        if new is not None:
            await new.aclose()
    _assert_new_turn_complete(events)
    nodes = _rows(
        "SELECT message FROM fluid_session_nodes WHERE session_id = %s", (sid,),
    )
    assert any("new turn" in str(row["message"]) for row in nodes)


async def test_real_delegated_engine_crossing_a_takeover():
    import sys
    from pathlib import Path

    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import UserTurn

    from app.core.provider.fluid_pg_store import PgFluidStore

    fake = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"

    def loop(store, session_id):
        return DelegatedLoop(
            store=store, model="claude-fake", claude_cmd=[sys.executable, str(fake)],
            session_id=session_id, partial=True,
            env={"FAKE_CLAUDE_SCENARIO": "s", "FAKE_CLAUDE_DELTAS": "12,4"},
        )

    old_store = PgFluidStore()
    sid = f"own-delegated-{uuid4().hex[:8]}"
    old = loop(old_store, sid)
    new_store = PgFluidStore()
    new = None
    handle = None

    async def new_turn():
        nonlocal new, handle
        await new_store.claim_session(sid)
        new = loop(new_store, sid)
        await new.submit(UserTurn(content="new turn"))
        await asyncio.wait_for(new.wait_turn(), 60)
        if new._cli_id_writes:
            await asyncio.wait(set(new._cli_id_writes), timeout=10)
        handle = _session_row(sid)["cli_session_id"]

    try:
        events = await _turn_crossing_a_takeover(old, old_store, new_turn, sid)
        if old._cli_id_writes:
            await asyncio.wait(set(old._cli_id_writes), timeout=10)
    finally:
        await old.aclose()
        if new is not None:
            await new.aclose()
    _assert_new_turn_complete(events)
    assert handle and _session_row(sid)["cli_session_id"] == handle


# ─── The takeover point and the reuse gate ───────────────────────────────────

def test_every_reuse_gate_checks_ownership_first():
    """Structural: running each gate for real needs a whole streaming turn."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "core"
    gates = 0
    for name in ("query_engine.py", "query_background.py"):
        lines = (root / name).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.strip() == "existing_state = pool.get(session_id)":
                gates += 1
                assert lines[index - 1].strip() == "await retire_if_replaced(pool, session_id)", (
                    f"{name}:{index + 1}"
                )
    assert gates == 3


def _adapter_with(store):
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter()
    adapter._engine, adapter._connected, adapter._store = object(), True, store
    return adapter


@pytest.mark.parametrize("field", ["session_id", "resume_session_id"])
@pytest.mark.parametrize("delegated", [True, False])
async def test_every_new_engine_takes_over_in_connect(monkeypatch, field, delegated):
    """``connect`` builds every engine, so each pool route that drops one ends here.

    Reaching the engine constructor shows the claim came first, on both legs.
    """
    from fluid.tools.registry import ToolRegistry

    from app.core.provider import harness_adapter as ha
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.provider.types import SessionOptions

    class Built(Exception):
        pass

    old, sid = await _session()
    before = _session_row(sid)["writer_generation"]
    new = PgFluidStore()

    def stop_at_engine(*_args, **_kwargs):
        assert _session_row(sid)["writer_generation"] == before + 1
        raise Built

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    monkeypatch.setattr(ha, "DelegatedLoop", stop_at_engine)
    # The native leg's first step towards its engine: this needs no credential.
    monkeypatch.setattr(ha, "_build_provider_client", stop_at_engine)
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: new))
    options = SessionOptions(
        model="claude-fake" if delegated else "scripted-native",
        setting_sources=[], extra={"fluid_registry": ToolRegistry()}, **{field: sid},
    )
    adapter = ha.HarnessAdapter()
    with pytest.raises(Built):
        await adapter.connect(options)
    assert adapter._is_delegated is delegated

    await old.append_event(sid, _event(sid, 2, "old"))
    assert _events(sid) == {1: "first"}


async def test_failed_takeover_fails_the_connect_rather_than_sharing_the_session(monkeypatch):
    from app.core.provider import fluid_pg_store as module
    from app.core.provider.fluid_pg_store import PgFluidStore

    monkeypatch.setattr(module, "_CLAIM_LOCK_TIMEOUT_MS", 100)
    old, sid = await _session()
    late, hold = await _held(old.append_event(sid, _event(sid, 2, "old")), "commit")

    new = PgFluidStore()
    with pytest.raises(Exception, match="lock"):
        await new.claim_session(sid)
    hold.release.set()
    await late

    await new.claim_session(sid)
    assert old.lost_ownership() is False  # Nothing refused it yet: it committed first.
    assert _events(sid) == {1: "first", 2: "old"}


async def test_abandoned_takeover_does_not_lock_out_the_retry():
    """A cancelled connect's thread still runs, and must not bump past its retry."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    _, sid = await _session()
    before = _session_row(sid)["writer_generation"]
    abandoned = PgFluidStore()
    claiming, hold = await _held(abandoned.claim_session(sid), "before")
    claiming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claiming

    retry = PgFluidStore()
    await retry.claim_session(sid)
    hold.release.set()
    assert await asyncio.to_thread(hold.finished.wait, 10)
    assert _session_row(sid)["writer_generation"] == before + 1

    await retry.append_event(sid, _event(sid, 2, "retry"))
    assert _events(sid)[2] == "retry"
    assert retry.lost_ownership() is False


class _Pool:
    """The two calls the reuse gate makes on the real pool."""

    def __init__(self, session_id, state):
        self.session_id, self.state, self.removed = session_id, state, []

    def get(self, session_id):
        return self.state if session_id == self.session_id else None

    async def remove(self, session_id, *, preserve_session=False):
        self.removed.append((session_id, preserve_session))
        self.state = None


async def _pooled(store, sid, *, streaming=False):
    from types import SimpleNamespace

    state = SimpleNamespace(adapter=_adapter_with(store), is_streaming=streaming)
    return state, _Pool(sid, state)


async def test_pooled_client_replaced_while_idle_is_closed_rather_than_reused():
    """Its writes would all be refused, so reusing it would lose the whole turn."""
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.session_pool import retire_if_replaced

    old, sid = await _session()
    _, pool = await _pooled(old, sid)

    await retire_if_replaced(pool, sid)
    assert pool.removed == []

    await PgFluidStore().claim_session(sid)
    await retire_if_replaced(pool, sid)
    # The normal close, not the dead-process release: its CLI is still running.
    assert pool.removed == [(sid, True)]


async def test_replaced_client_mid_turn_still_counts_as_alive_and_stays_pooled():
    """``evict_dead`` reads ``is_alive`` as a dead process and would end the turn."""
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.session_pool import is_subprocess_alive, retire_if_replaced

    old, sid = await _session()
    state, pool = await _pooled(old, sid, streaming=True)
    await PgFluidStore().claim_session(sid)
    await old.append_event(sid, _event(sid, 2, "refused"))
    assert old.lost_ownership() is True

    await retire_if_replaced(pool, sid)
    assert pool.removed == []
    assert is_subprocess_alive(state) is True


async def test_turn_starting_during_the_check_keeps_its_client():
    from types import SimpleNamespace

    from app.core.session_pool import retire_if_replaced

    async def replaced():
        state.is_streaming = True
        return True

    state = SimpleNamespace(
        adapter=SimpleNamespace(replaced_by_another_engine=replaced), is_streaming=False,
    )
    pool = _Pool("sid", state)
    await retire_if_replaced(pool, "sid")
    assert pool.removed == []


async def test_a_session_row_that_has_gone_is_not_a_takeover():
    from app.core.session_pool import retire_if_replaced
    from app.db import base

    old, sid = await _session()
    _, pool = await _pooled(old, sid)

    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM fluid_sessions WHERE session_id = %s", (sid,))
    await retire_if_replaced(pool, sid)
    assert pool.removed == []


async def test_a_failed_ownership_check_does_not_fail_the_turn(monkeypatch):
    from app.core.provider import fluid_pg_store as module
    from app.core.session_pool import retire_if_replaced

    old, sid = await _session()
    _, pool = await _pooled(old, sid)

    def broken(_session_ids):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module, "_writer_generations_sync", broken)
    await retire_if_replaced(pool, sid)
    assert pool.removed == []


# ─── Other writers to the same session ───────────────────────────────────────

async def test_handle_writes_from_one_engine_land_in_order():
    """Refused --resume: the erase must not overtake the handle that replaces it."""
    old, sid = await _session()
    await old.set_cli_session_id(sid, "cli-refused")
    erase, hold = await _held(old.set_cli_session_id(sid, None), "before")
    replacement = asyncio.ensure_future(old.set_cli_session_id(sid, "cli-fresh"))
    await asyncio.sleep(0.2)

    hold.release.set()
    await erase
    await replacement
    assert _session_row(sid)["cli_session_id"] == "cli-fresh"


async def test_readers_and_session_search_do_not_disturb_the_writer():
    """``app/mcp/builtin_tools.py`` builds a store of its own for every search."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    owner, sid = await _session()
    before = _session_row(sid)["writer_generation"]

    reader = PgFluidStore()
    assert await reader.last_event_seq(sid) == 1
    assert (await reader.get_session(sid)) is not None
    assert [event.seq async for event in reader.iter_events(sid, 0)] == [1]

    assert _session_row(sid)["writer_generation"] == before
    await owner.append_event(sid, _event(sid, 2, "still mine"))
    assert _events(sid)[2] == "still mine"
    assert owner.lost_ownership() is False


async def test_a_store_that_never_claimed_still_writes():
    """Scripts and older callers have no generation, so nothing refuses them."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    _, sid = await _session()
    unclaimed = PgFluidStore()
    await unclaimed.get_session(sid)
    await unclaimed.set_cli_session_id(sid, "cli-script")
    await unclaimed.append_event(sid, _event(sid, 2, "script"))
    assert _session_row(sid)["cli_session_id"] == "cli-script"
    assert _events(sid)[2] == "script"


async def test_fork_leaves_the_parent_writer_alone_and_gets_its_own_owner():
    from app.core.provider import fluid_transcript
    from app.core.provider.fluid_pg_store import PgFluidStore

    parent, sid = await _session()
    await parent.append(sid, _node("n1", None))
    before = _session_row(sid)["writer_generation"]

    fork_id, _ = await asyncio.to_thread(fluid_transcript.fork_fluid_session, sid, "n1")

    assert _session_row(sid)["writer_generation"] == before
    await parent.append_event(sid, _event(sid, 2, "parent"))
    assert _events(sid)[2] == "parent"
    assert parent.lost_ownership() is False

    child = PgFluidStore()
    await _take_over(child, fork_id)
    meta = await child.get_session(fork_id)
    assert meta.head_node_id == "n1"
    await child.append(fork_id, _node("child-node", "n1"))
    assert _session_row(fork_id)["head_node_id"] == "child-node"
    assert _session_row(sid)["head_node_id"] == "n1"


async def test_second_creator_of_a_session_takes_it_over():
    """Two engines creating one id would otherwise both pass the check."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    first, second = PgFluidStore(), PgFluidStore()
    sid = f"own-{uuid4().hex[:10]}"
    await second.claim_session(sid)  # No row yet: nothing to take over.
    await first.create_session(sid)
    await second.create_session(sid)

    await second.append_event(sid, _event(sid, 1, "second"))
    await first.append_event(sid, _event(sid, 1, "first"))
    assert _events(sid) == {1: "second"}
    assert first.lost_ownership() is True


async def test_two_engines_inserting_one_session_do_not_share_it():
    """Both found no row, so the second insert hits the conflict and takes over."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    first, second = PgFluidStore(), PgFluidStore()
    sid = f"own-{uuid4().hex[:10]}"
    assert await second.get_session(sid) is None
    await first.create_session(sid)
    await second.create_session(sid)

    await first.append_event(sid, _event(sid, 1, "first"))
    await second.append_event(sid, _event(sid, 1, "second"))
    assert _events(sid) == {1: "second"}
    assert first.lost_ownership() is True
    assert second.lost_ownership() is False


async def test_rewind_ends_the_previous_engines_ownership():
    """Eviction is best effort, so the rewind itself must stop the old engine."""
    from app.core.provider import fluid_transcript

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    await old.append(sid, _node("n2", "n1"))

    result = await asyncio.to_thread(fluid_transcript.rewind_fluid_session, sid, "n1", False)
    assert result.success is True and result.writer_generation is not None

    await old.append(sid, _node("late", "n2"))
    head = _session_row(sid)["head_node_id"]
    assert head != "late"
    assert old.lost_ownership() is True


async def test_rewind_reads_the_old_engines_last_commit_before_cutting():
    """The claim waits for a write that is already committing."""
    from app.core.provider import fluid_transcript

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    write, hold = await _held(old.append(sid, _node("n2", "n1")), "commit")

    rewind = asyncio.ensure_future(
        asyncio.to_thread(fluid_transcript.rewind_fluid_session, sid, "n2", True),
    )
    await asyncio.sleep(0.2)
    assert not rewind.done(), "a rewind must wait for the write holding the row"

    hold.release.set()
    await write
    result = await rewind
    assert result.success is True, result.error


async def test_rewind_left_running_cannot_move_the_head_under_a_newer_connect(monkeypatch):
    """Its request was cancelled; its thread was not. The newer engine stands."""
    from app.core.provider import fluid_transcript
    from app.core.provider.fluid_pg_store import PgFluidStore

    old, sid = await _session()
    await old.append(sid, _node("n1", None))
    await old.append(sid, _node("n2", "n1"))

    claimed, proceed = threading.Event(), threading.Event()
    real = fluid_transcript.compute_rewind_keep_count

    def held(*args, **kwargs):
        claimed.set()
        assert proceed.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(fluid_transcript, "compute_rewind_keep_count", held)
    rewind = asyncio.ensure_future(
        asyncio.to_thread(fluid_transcript.rewind_fluid_session, sid, "n1", False),
    )
    while not claimed.is_set():
        await asyncio.sleep(0.005)

    newer = PgFluidStore()
    await _take_over(newer, sid)
    await newer.get_session(sid)
    proceed.set()
    result = await rewind

    assert result.success is False and "continued" in result.error
    await newer.append(sid, _node("n3", "n2"))
    assert newer.lost_ownership() is False
    assert _session_row(sid)["head_node_id"] == "n3"


async def _store_owning(sid: str):
    from app.core.provider.fluid_pg_store import PgFluidStore

    store = PgFluidStore()
    await _take_over(store, sid)
    await store.get_session(sid)
    return store


async def test_abandoned_reseed_cannot_overwrite_the_retrys_handle(monkeypatch, tmp_path):
    """A connect timed out mid re-seed, a retry claimed and saved its handle, and
    then the first re-seed reached its own handle write."""
    from app.core.provider import fluid_transcript

    first, sid = await _session()
    await first.append(sid, _node("n1", None))
    abandoned = first.claimed_generation(sid)

    writing, proceed = threading.Event(), threading.Event()
    real = fluid_transcript._write_cli_transcript

    def held(*args, **kwargs):
        writing.set()
        assert proceed.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(fluid_transcript, "_write_cli_transcript", held)
    reseed = asyncio.ensure_future(asyncio.to_thread(
        fluid_transcript.reseed_delegated_cli_session, sid, str(tmp_path), tmp_path,
        force=True, generation=abandoned,
    ))
    while not writing.is_set():
        await asyncio.sleep(0.005)

    retry = await _store_owning(sid)
    await retry.set_cli_session_id(sid, "cli-from-the-retry")
    proceed.set()
    result = await reseed

    assert result.status == "superseded" and result.cli_session_id is None
    assert _session_row(sid)["cli_session_id"] == "cli-from-the-retry"


async def test_abandoned_reseed_of_an_empty_session_cannot_clear_the_handle(tmp_path):
    from app.core.provider import fluid_transcript

    first, sid = await _session()
    abandoned = first.claimed_generation(sid)
    retry = await _store_owning(sid)
    await retry.set_cli_session_id(sid, "cli-from-the-retry")

    result = await asyncio.to_thread(
        fluid_transcript.reseed_delegated_cli_session, sid, str(tmp_path), tmp_path,
        force=True, generation=abandoned,
    )

    assert result.status == "superseded"
    assert _session_row(sid)["cli_session_id"] == "cli-from-the-retry"


def _archive_row() -> dict:
    return {"node_id": "archived", "role": "user", "text": "kept for HistoryExpand"}


async def test_abandoned_reseed_cannot_take_an_event_number_from_its_replacement():
    """Its replacement seeds its numbering from the stored maximum, then issues
    numbers in memory, so a late archive event would take one and drop its event."""
    from app.core.provider import fluid_transcript

    first, sid = await _session()
    abandoned = first.claimed_generation(sid)
    retry = await _store_owning(sid)
    before = _events_raw(sid)

    with pytest.raises(fluid_transcript.SessionSuperseded):
        await asyncio.to_thread(
            fluid_transcript._append_archive, sid, [_archive_row()], generation=abandoned,
        )

    assert _events_raw(sid) == before
    await retry.append_event(sid, _event(sid, before[-1][0] + 1, "from the retry"))
    assert len(_events_raw(sid)) == len(before) + 1


async def test_a_claim_waits_for_an_archive_event_still_committing(monkeypatch):
    """So the replacement's numbering starts after it rather than on top of it."""
    from app.core.provider import fluid_transcript
    from app.db import base

    first, sid = await _session()
    generation = first.claimed_generation(sid)
    hold = Hold("commit")
    real = base.get_db

    @contextmanager
    def held_db(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            yield conn
            if threading.current_thread() is not threading.main_thread():
                hold.stop("commit")

    monkeypatch.setattr(base, "get_db", held_db)
    archive = asyncio.ensure_future(asyncio.to_thread(
        fluid_transcript._append_archive, sid, [_archive_row()], generation=generation,
    ))
    while not hold.reached.is_set():
        await asyncio.sleep(0.005)
    monkeypatch.setattr(base, "get_db", real)

    from app.core.provider.fluid_pg_store import PgFluidStore

    retry = PgFluidStore()
    claim = asyncio.ensure_future(_take_over(retry, sid))
    await asyncio.sleep(0.2)
    assert not claim.done(), "the claim must wait for the archive's transaction"

    hold.release.set()
    await archive
    await claim
    assert len(_events_raw(sid)) == 2


async def test_abandoned_reseed_stops_at_compaction_and_writes_no_transcript(monkeypatch, tmp_path):
    """"Writing it whole" is for a failed compaction, not for a session it lost."""
    from fluid.engine import claude_mod_compaction

    from app.core.provider import fluid_transcript

    first, sid = await _session()
    await first.append(sid, _node("n1", None))
    await first.append(sid, _node("n2", "n1"))
    abandoned = first.claimed_generation(sid)
    retry = await _store_owning(sid)
    await retry.set_cli_session_id(sid, "cli-from-the-retry")
    before = _events_raw(sid)

    def compact(rows, nodes, *, context_window, trigger, archive):
        archive.append(_archive_row())
        return rows[-1:]

    monkeypatch.setattr(claude_mod_compaction, "compact_messages", compact)
    monkeypatch.setattr(fluid_transcript, "reseed_compaction_threshold", lambda _window: 1)
    written = MagicMock()
    monkeypatch.setattr(fluid_transcript, "_write_cli_transcript", written)

    result = await asyncio.to_thread(
        fluid_transcript.reseed_delegated_cli_session, sid, str(tmp_path), tmp_path,
        force=True, context_window=1000, generation=abandoned,
    )

    assert result.status == "superseded"
    written.assert_not_called()
    assert _events_raw(sid) == before
    assert _session_row(sid)["cli_session_id"] == "cli-from-the-retry"


async def test_current_owner_reseeds_and_archives(monkeypatch, tmp_path):
    """The same path with a live claim, so the refusals above mean something."""
    from fluid.engine import claude_mod_compaction

    from app.core.provider import fluid_transcript

    owner, sid = await _session()
    await owner.append(sid, _node("n1", None))
    await owner.append(sid, _node("n2", "n1"))

    def compact(rows, nodes, *, context_window, trigger, archive):
        archive.append(_archive_row())
        return rows[-1:]

    monkeypatch.setattr(claude_mod_compaction, "compact_messages", compact)
    monkeypatch.setattr(fluid_transcript, "reseed_compaction_threshold", lambda _window: 1)

    result = await asyncio.to_thread(
        fluid_transcript.reseed_delegated_cli_session, sid, str(tmp_path), tmp_path,
        force=True, context_window=1000, generation=owner.claimed_generation(sid),
    )

    assert result.status == "reseeded"
    assert _session_row(sid)["cli_session_id"] == result.cli_session_id
    assert len(_events_raw(sid)) == 2


async def test_connect_reseeds_under_its_own_claim(monkeypatch):
    """The generation that reaches the re-seed is this connect's."""
    from types import SimpleNamespace

    from app.core.provider import fluid_transcript, harness_adapter

    owner, sid = await _session()
    seen: dict = {}

    def reseed(*_args, **kwargs):
        seen.update(kwargs)
        return fluid_transcript.ReseedResult(status="failed", error="stop here")

    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", reseed)
    monkeypatch.setattr(harness_adapter, "_delegated_context_window", lambda _options: 1000)
    adapter = _adapter_with(owner)

    await adapter._reseed_cli_session(sid, SimpleNamespace(cwd="/tmp", env={}))

    assert seen["generation"] == owner.claimed_generation(sid)
    assert seen["generation"] is not None


async def test_handle_write_without_a_claim_still_lands():
    """A fork's re-seed: nothing else can reach its brand-new session."""
    from app.core.provider import fluid_transcript

    _, sid = await _session()
    await asyncio.to_thread(fluid_transcript._set_cli_session_id, sid, "cli-reseeded")
    assert _session_row(sid)["cli_session_id"] == "cli-reseeded"


async def test_engine_loses_a_turn_to_another_install_and_says_so():
    """Admission cannot see another install. Real stores: nothing here refuses, the
    takeover lands mid-turn, and the turn must not finish as a success."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fluid.events.model import TurnComplete

    from app.core.provider import session_admission
    from app.core.provider.session_admission import SessionReplacedError

    owner, sid = await _session()
    adapter = _adapter_with(owner)
    session_admission.admit(sid, adapter)

    async def stream(since_seq=0):
        await _store_owning(sid)  # another install, on the same database
        yield TurnComplete(
            session_id=sid, turn_id="t1", seq=9, stop_reason="end_turn", num_model_calls=1,
        )

    adapter._engine = SimpleNamespace(events=stream, submit=AsyncMock())

    with pytest.raises(SessionReplacedError):
        async for _ in adapter.receive_events():
            pass
    adapter._engine.submit.assert_awaited_once()


def test_handoff_never_carries_the_generation():
    """It belongs to this install's engines. Export is by explicit column list."""
    from app.network import handoff

    assert "writer_generation" not in handoff.TABLES["fluid_sessions"]


# ─── Upgrade and rollback ────────────────────────────────────────────────────

@pytest.fixture
def _column_back_afterwards():
    """A failure here must not leave every later test without the column."""
    from app.db import base

    yield
    with base.get_db() as conn:
        conn.cursor().execute(
            "ALTER TABLE fluid_sessions ADD COLUMN IF NOT EXISTS "
            "writer_generation BIGINT NOT NULL DEFAULT 0"
        )


async def test_upgrade_from_a_build_without_the_column_and_back(_column_back_afterwards):
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.db import base, database

    _, sid = await _session()
    with base.get_db() as conn:
        conn.cursor().execute("ALTER TABLE fluid_sessions DROP COLUMN writer_generation")
    # The older build's writes, against the older schema.
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE fluid_sessions SET head_node_id = %s WHERE session_id = %s", ("legacy", sid),
        )

    with base.get_db() as conn:
        database._run_column_migrations(conn.cursor())

    row = _session_row(sid)
    assert row["writer_generation"] == 0
    assert row["head_node_id"] == "legacy"

    # An existing session is taken over like any other.
    new = PgFluidStore()
    await new.claim_session(sid)
    assert _session_row(sid)["writer_generation"] == 1
    await new.append_event(sid, _event(sid, 2, "after upgrade"))
    assert _events(sid)[2] == "after upgrade"

    # Rollback: the older build's statements name no such column and still work,
    # including on a row a newer build has already taken over.
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at, cli_session_id, "
            "project_root) VALUES (%s, NULL, 0, NULL, NULL) ON CONFLICT (session_id) DO NOTHING",
            (f"older-{sid}",),
        )
        cursor.execute(
            "UPDATE fluid_sessions SET cli_session_id = %s WHERE session_id = %s",
            ("cli-older-build", sid),
        )
    assert _session_row(f"older-{sid}")["writer_generation"] == 0
    assert _session_row(sid)["cli_session_id"] == "cli-older-build"
