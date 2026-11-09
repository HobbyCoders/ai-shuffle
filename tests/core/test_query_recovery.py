"""Durable query receipts survive a kill and never duplicate terminal history."""
from contextlib import ExitStack, asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import asyncio
from threading import Event
from unittest.mock import AsyncMock

import pytest

from app.db import database, query_recovery, turns
from app.db.base import get_db
from tests.pg_shim import route_db_to


@pytest.fixture
def session():
    return database.create_session(session_id="recovery-session", profile_id="default")


def rows(table):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table}")
        return cursor.fetchall()


def receipt(session, **kwargs):
    return query_recovery.begin(
        session["id"], "api_query", instance_id="this-install", process_id="old-process",
        prompt="remember this query", profile_id="default", **kwargs,
    )


def recover():
    return query_recovery.recover(instance_id="this-install", process_id="new-process")


def test_old_process_receipt_recovers_one_aborted_terminal(session):
    saved = receipt(session, overrides={"model": "test"}, payload={"api_user_id": "user"})
    assert not rows("turns")  # Not live/queued work; the scheduler cannot run it.
    assert recover() == 1
    terminal, = rows("turns")
    assert terminal["id"] == saved["id"]
    assert terminal["state"] == "aborted"
    assert terminal["stop_reason"] == "server_shutdown"
    assert terminal["prompt"] == "remember this query"
    assert json.loads(terminal["overrides"]) == {"model": "test"}
    assert json.loads(terminal["payload"]) == {"api_user_id": "user"}
    assert not rows("query_recovery")
    assert recover() == 0
    assert len(rows("turns")) == 1


def test_current_process_and_other_install_receipts_are_not_recovered(session):
    receipt(session)
    query_recovery.begin(session["id"], "api_query", instance_id="other-install", process_id="old")
    query_recovery.begin(session["id"], "api_query", instance_id="this-install", process_id="new-process")
    assert recover() == 1
    assert len(rows("query_recovery")) == 2


def test_normal_completion_and_replay_share_one_terminal_identity(session):
    saved = receipt(session)
    terminal = turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"], outcome="completed")
    assert terminal["state"] == "completed"
    assert not rows("query_recovery")
    assert recover() == 0
    # Even a stale recovery reader which fetched before that commit cannot
    # overwrite the completion or charge its work interval a second time.
    repeated = turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"], outcome="aborted")
    assert repeated["state"] == "completed"
    assert len(rows("turns")) == 1


def test_terminal_and_receipt_removal_are_atomic(session, monkeypatch):
    saved = receipt(session)
    real_get_db = turns.get_db

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM query_recovery"):
                raise RuntimeError("failed before receipt deletion")
            return self.cursor.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self):
            return Cursor(self.conn.cursor())

    @contextmanager
    def failing_db():
        with real_get_db() as conn:
            yield Connection(conn)

    with monkeypatch.context() as patch:
        patch.setattr(turns, "get_db", failing_db)
        with pytest.raises(RuntimeError, match="before receipt deletion"):
            turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"])
    assert not rows("turns")
    assert len(rows("query_recovery")) == 1
    assert recover() == 1
    assert len(rows("turns")) == 1


def test_pre_upgrade_history_is_unchanged_and_schema_upgrade_is_idempotent(session):
    old = turns.record_adhoc_turn(session["id"], "api_query", outcome="completed", prompt="old work")
    with get_db() as conn:
        query_recovery.create_schema(conn.cursor())
        query_recovery.create_schema(conn.cursor())
    assert recover() == 0
    assert rows("turns")[0]["id"] == old["id"]
    assert rows("turns")[0]["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming,cancel_begin", [(False, False), (True, False), (False, True)])
async def test_real_query_produces_receipt_before_provider_and_consumes_it(session, tmp_path, monkeypatch, streaming, cancel_begin):
    from app.core import credential_gate, query_engine as qe, query_shutdown, sdk_options
    from app.core.provider.events import ResultMessage
    from app.db.async_database import run_sync
    from tests.core.query_engine_flow_harness import engine_patches, fresh_adapter

    monkeypatch.setattr(query_shutdown, "_closing_loop", None)
    seen, identities = [], []
    begun, terminal_written = asyncio.Event(), asyncio.Event()
    release_begin = Event()
    loop = asyncio.get_running_loop()

    async def sent(*args):
        pending, = rows("query_recovery")
        seen.append(pending["id"])
        assert not rows("turns")

    @asynccontextmanager
    async def no_plan(*args, **kwargs):
        yield None

    with ExitStack() as stack:
        adapter = fresh_adapter([ResultMessage(subtype="success")])
        adapter.send_query.side_effect = sent
        boundaries = engine_patches(stack, cwd=str(tmp_path), adapter=adapter, session_row=session)
        monkeypatch.setattr(sdk_options, "build_session_options", lambda **kwargs: (boundaries.options, {}))
        monkeypatch.setattr(credential_gate, "refusal_for_options_async", AsyncMock(return_value=None))
        monkeypatch.setattr(qe, "recorded_turn", no_plan)
        monkeypatch.setattr(qe, "_record_query_message", AsyncMock())
        patched_sync = qe.run_sync

        async def dispatch(fn, *args, **kwargs):
            if fn is query_recovery.begin:
                identities.append(kwargs["turn_id"])
                if cancel_begin:
                    def delayed_begin():
                        loop.call_soon_threadsafe(begun.set)
                        assert release_begin.wait(3)
                        return fn(*args, **kwargs)
                    return await run_sync(delayed_begin)
                return await run_sync(fn, *args, **kwargs)
            if fn is turns.record_adhoc_turn:
                result = await run_sync(fn, *args, **kwargs)
                terminal_written.set()
                return result
            return await patched_sync(fn, *args, **kwargs)

        monkeypatch.setattr(qe, "run_sync", dispatch)
        if cancel_begin:
            owner = asyncio.create_task(qe.execute_query("do work", "default", session_id=session["id"]))
            try:
                await asyncio.wait_for(begun.wait(), 2)
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
                await asyncio.wait_for(terminal_written.wait(), 2)
            finally:
                release_begin.set()
                await asyncio.wait_for(asyncio.gather(*query_shutdown._owned), 3)
            # The issued receipt can commit AFTER the normal terminal. Its
            # identity must still deduplicate when a subsequent boot replays it.
            query_recovery.recover(process_id="next-process")
            adapter.send_query.assert_not_awaited()
        elif streaming:
            async for _ in qe.stream_query("do work", "default", session_id=session["id"]):
                pass
        else:
            await qe.execute_query("do work", "default", session_id=session["id"])
    assert len(seen) == (0 if cancel_begin else 1)
    terminal, = rows("turns")
    assert terminal["id"] == identities[0]
    assert terminal["state"] == ("aborted" if cancel_begin else "completed")
    assert not rows("query_recovery")


KILLED_WRITER = r'''
from contextlib import contextmanager
from pathlib import Path
import sqlite3, sys, time
from app.db import query_recovery, turns
from tests.pg_shim import route_db_to
raw = sqlite3.connect(sys.argv[1])
raw.row_factory = sqlite3.Row
with route_db_to(raw):
    saved = query_recovery.begin("recovery-session", "api_query", instance_id="this-install",
                                process_id="old-process", prompt="interrupted writer")
    original = turns.get_db
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM query_recovery"):
                Path(sys.argv[2]).touch()  # Terminal INSERT issued but NOT committed.
                time.sleep(60)
            return self.cursor.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.cursor, name)
    class Connection:
        def __init__(self, conn): self.conn = conn
        def cursor(self): return Cursor(self.conn.cursor())
    @contextmanager
    def delayed_db():
        with original() as conn: yield Connection(conn)
    turns.get_db = delayed_db
    turns.record_adhoc_turn("recovery-session", "api_query", turn_id=saved["id"], outcome="aborted")
'''


def test_killed_process_mid_terminal_transaction_recovers_on_restart(session, test_db, tmp_path):
    # A portable process kill (also Windows), not a simulated cancelled coroutine.
    filename, marker = tmp_path / "recovery.sqlite", tmp_path / "insert-issued"
    with sqlite3.connect(filename) as target:
        test_db.backup(target)
    script = tmp_path / "killed_writer.py"
    script.write_text(KILLED_WRITER)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    process = subprocess.Popen([sys.executable, str(script), str(filename), str(marker)], env=env)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert process.poll() is None, "writer failed before issuing its terminal"
            assert time.monotonic() < deadline, "writer did not reach delayed finalization"
            time.sleep(.01)
        process.kill()
        process.wait(timeout=5)
        raw = sqlite3.connect(filename)
        raw.row_factory = sqlite3.Row
        try:
            with route_db_to(raw):
                assert not rows("turns")
                assert len(rows("query_recovery")) == 1
                assert recover() == 1
                assert rows("turns")[0]["state"] == "aborted"
                assert recover() == 0
        finally:
            raw.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
