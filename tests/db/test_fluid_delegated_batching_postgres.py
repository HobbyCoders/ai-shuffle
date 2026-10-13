"""Opt-in delegated batching contracts on disposable PostgreSQL (#1284).

A separate producer process (``delegated_crash_producer.py``) runs a real
``DelegatedLoop`` turn against the fake stream-json CLI and exits abruptly at a
buffered, batch or terminal boundary; a fresh store and loop in this process
then resume the session. The COMMIT-failure cases use a deferred constraint
trigger, so the error arrives from PostgreSQL's own COMMIT.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401  (pg_schema: autouse fixture)

pytestmark = requires_real_pg

ROOT = Path(__file__).resolve().parents[2]
PRODUCER = Path(__file__).with_name("delegated_crash_producer.py")
FAKE = ROOT / "fluid" / "tests" / "fixtures" / "fake_claude.py"


def _loop(store, session_id, *, deltas="40,8"):
    from fluid.engine.delegated import DelegatedLoop
    return DelegatedLoop(
        store=store, model="claude-fake", claude_cmd=[sys.executable, str(FAKE)],
        session_id=session_id, partial=True,
        env={"FAKE_CLAUDE_SCENARIO": "s", "FAKE_CLAUDE_DELTAS": deltas},
    )


async def _durable(sid):
    from app.core.provider.fluid_pg_store import PgFluidStore
    return [e async for e in PgFluidStore().iter_events(sid, 0)]


def _sql(statement, params=()):
    from app.db import base
    with base.get_db() as conn:
        conn.cursor().execute(statement, params)


@pytest.mark.parametrize("boundary,exit_after,deltas_kept", [
    ("buffered", 5, 0),
    ("batch", 37, 32),
    ("terminal", 0, 48),
])
async def test_abrupt_producer_exit_keeps_committed_prefix_and_restart_continues(
    tmp_path, boundary, exit_after, deltas_kept,
):
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.events import UserTurn
    from fluid.events.model import BlockStart, TextDelta, TurnComplete, TurnStarted

    sid = f"crash-{boundary}-{uuid4().hex[:8]}"
    env = dict(os.environ, DATA_DIR=str(tmp_path), TESTING="1",
               PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "fluid")]))
    child = await asyncio.to_thread(
        subprocess.run, [sys.executable, str(PRODUCER), sid, boundary, str(exit_after)],
        cwd=str(ROOT), env=env, capture_output=True, timeout=120,
    )
    assert child.returncode == 17, child.stderr.decode(errors="replace")[-2000:]

    kept = await _durable(sid)
    assert [e.seq for e in kept] == list(range(1, len(kept) + 1))
    assert sum(isinstance(e, TextDelta) for e in kept) == deltas_kept
    if boundary == "terminal":
        assert isinstance(kept[-1], TurnComplete)
    else:
        # Only reconstructible deltas were lost: every durable event before
        # the first one survived, and nothing durable follows the kept tail.
        first = next(i for i, e in enumerate(kept + [None]) if isinstance(e, TextDelta) or e is None)
        assert isinstance(kept[0], TurnStarted) and isinstance(kept[first - 1], BlockStart)
        assert all(isinstance(e, TextDelta) for e in kept[first:])
    head = await PgFluidStore().last_event_seq(sid)
    assert head == len(kept)

    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60
    resumed = _loop(store, sid, deltas="3,1")
    try:
        await resumed.submit(UserTurn(content="after restart"))
        await asyncio.wait_for(resumed.wait_turn(), 60)
        published = resumed.bus.log(sid)
    finally:
        await resumed.aclose()
    assert published[0].seq == head + 1
    replay = await _durable(sid)
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert [(e.seq, type(e)) for e in replay[:head]] == [(e.seq, type(e)) for e in kept]
    assert isinstance(replay[-1], TurnComplete)


@pytest.fixture
def reject_on_commit():
    """Install a deferred trigger that fails COMMIT for one session's rows."""
    installed = []

    def install(session_id, condition):
        _sql(
            "CREATE OR REPLACE FUNCTION reject_fluid_event_for_test() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"IF NEW.session_id = '{session_id}' AND ({condition}) THEN "
            "RAISE EXCEPTION 'injected commit failure'; END IF; RETURN NULL; END $$"
        )
        _sql(
            "CREATE CONSTRAINT TRIGGER reject_fluid_event AFTER INSERT ON fluid_session_events "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_fluid_event_for_test()"
        )
        installed.append(True)

    yield install
    if installed:
        _sql("DROP TRIGGER IF EXISTS reject_fluid_event ON fluid_session_events")
        _sql("DROP FUNCTION IF EXISTS reject_fluid_event_for_test()")


async def test_terminal_commit_failure_surfaces_without_phantom_terminal(reject_on_commit):
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.events import UserTurn
    from fluid.events.model import ErrorEvent, TurnComplete

    sid = f"commit-terminal-{uuid4().hex[:8]}"
    reject_on_commit(sid, "NEW.event->>'_cls' = 'TurnComplete'")
    store = PgFluidStore()
    loop = _loop(store, sid)
    try:
        await loop.submit(UserTurn(content="go"))
        await asyncio.wait_for(loop.wait_turn(), 60)
        published = loop.bus.log(sid)
    finally:
        await loop.aclose()

    terminal = next(e for e in published if isinstance(e, TurnComplete))
    assert isinstance(published[-1], ErrorEvent)
    assert "injected commit failure" in published[-1].message
    replay = await _durable(sid)
    assert not any(isinstance(e, TurnComplete) for e in replay)
    assert [e.seq for e in replay] == [e.seq for e in published if e.seq != terminal.seq]
    assert not any(isinstance(e, TurnComplete) for e in await store.events_since(sid, 0))


async def test_batched_tail_commit_failure_surfaces_without_phantom_events(reject_on_commit):
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.events import UserTurn
    from fluid.events.model import BlockStop, ErrorEvent, TextDelta

    sid = f"commit-tail-{uuid4().hex[:8]}"
    # a35 is in the 8-delta tail after the first full batch; the BlockStop
    # barrier carries that tail, so its COMMIT is the one that fails.
    reject_on_commit(sid, "NEW.event->>'text' = 'a35 '")
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60
    loop = _loop(store, sid)
    try:
        await loop.submit(UserTurn(content="go"))
        with pytest.raises(Exception, match="injected commit failure"):
            await asyncio.wait_for(loop.wait_turn(), 60)
        published = loop.bus.log(sid)
    finally:
        await loop.aclose()

    assert isinstance(published[-1], ErrorEvent)  # Live subscribers saw the failure.
    first_delta = next(e.seq for e in published if isinstance(e, TextDelta))
    committed = await _durable(sid)
    assert [e.seq for e in committed] == list(range(1, first_delta + 32))
    assert not any(isinstance(e, BlockStop) and e.seq >= first_delta for e in committed)
    assert sid not in store._events  # No cached copy of the rejected transaction.
    assert len(store._pending_events[sid]) == 8  # Accepted deltas await a retry.

    _sql("DROP TRIGGER reject_fluid_event ON fluid_session_events")
    await store.flush_events(sid)
    recovered = await _durable(sid)
    assert [e.seq for e in recovered] == list(range(1, first_delta + 40))
    assert all(isinstance(e, TextDelta) for e in recovered[first_delta - 1:])
    assert await PgFluidStore().last_event_seq(sid) == first_delta + 39
