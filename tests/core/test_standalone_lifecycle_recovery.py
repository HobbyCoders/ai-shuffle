"""Rewind admission must survive a rolled-back standalone boundary write."""
import asyncio
import threading

from psycopg2.errors import LockNotAvailable

from app.core.provider import session_admission
from app.db import session_events
from tests.core.test_rewind_resolution import (  # noqa: F401
    SID, RewindRequestV2, _asst_ev, _patch_checkpoints, _user_ev, rewind_env,
)
from tests.rewind_helpers import execute_rewind_and_wait
from tests.shared_fixtures import seed_session


async def test_rewind_retries_before_releasing_admission(rewind_env, monkeypatch):
    seed_session(SID)
    session_events.append_events(SID, [
        _user_ev("first", uuid="u-1"), _asst_ev("reply one"),
        _user_ev("second", uuid="u-2"), _asst_ev("reply two"),
    ])
    _patch_checkpoints(monkeypatch, [])
    monkeypatch.setattr(session_events, "LOCK_RETRY_DELAYS_SECONDS", (0,))
    real = session_events.append_events
    entered, release = threading.Event(), threading.Event()
    attempts = 0

    def append(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LockNotAvailable()
        entered.set()
        assert release.wait(4)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_events, "append_events", append)
    task = asyncio.create_task(execute_rewind_and_wait(
        SID, RewindRequestV2(target_uuid="u-2", include_response=False),
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 2), "rewind boundary was not retried"
        assert SID in session_admission._rewinds
        assert not task.done()
    finally:
        release.set()
        response = await asyncio.wait_for(task, 4)
    assert response.success and response.chat_rewound
    assert attempts == 2
    assert SID not in session_admission._rewinds
    rows = session_events.get_events(SID)
    assert [r["event_type"] for r in rows].count("transcript.rewound") == 1
    assert rows[-1]["payload"]["from_seq"] == 3


async def test_archive_receipt_retries_before_marking_done(mock_db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.db import base, session_archive_migration
    from app.core import session_settlement

    seed_session("retirement")
    with base.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO session_archive_retirement "
            "(session_id,membership_revision,membership_epoch,cancelled_peers) "
            "VALUES (%s,1,'epoch','[\"peer\"]')", ("retirement",),
        )
    monkeypatch.setattr(session_events, "LOCK_RETRY_DELAYS_SECONDS", (0,))
    monkeypatch.setattr(session_settlement, "dismiss_owner_waits", AsyncMock())
    real = session_events.append_events
    attempts = 0

    def append(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT done FROM session_archive_retirement WHERE session_id='retirement'")
            assert not cursor.fetchone()["done"]
        if attempts == 1:
            raise LockNotAvailable()
        return real(*args, **kwargs)

    monkeypatch.setattr(session_events, "append_events", append)
    await session_archive_migration.reconcile()
    assert attempts == 2
    rows = session_events.get_events("retirement")
    assert len(rows) == 1 and rows[0]["event_type"] == "queue.cancelled"
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT done FROM session_archive_retirement WHERE session_id='retirement'")
        assert cursor.fetchone()["done"]
