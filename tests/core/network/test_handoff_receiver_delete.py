"""A chat deleted on the worker that just received it is deleted everywhere (#1308).

After a hand-off the receiver owns the chat, but the chat's sync record still
names the previous owner as its origin: the source's commit published the
ownership flip, and the receiver publishes the row itself only on its next
scan. The owner-only scan retracts only records this worker originated, so a
delete on the receiver before that scan published no tombstone, and the
previous owner kept the chat for good.

When the source's commit record reaches the receiver only after that delete,
it must not outrank the tombstone and bring the chat back on both workers:
the receiver observes the commit's clock from the commit reply, or from the
source's finalize-wakes when every reply was lost. The delete and its
tombstone commit together, so the commit record cannot land in between, and
a chat moved from a local mirror with no sync record is still retracted.
"""
import time

import pytest
from fastapi import BackgroundTasks

from app.api import sessions as sessions_api
from app.core import session_handoff
from app.db import database, session_moves, wakeups
from app.db import network as store
from app.db import sessions as sessions_db
from app.db.base import get_db
from app.network import catalog, control, handoff
from tests.core.network.harness import converge, exchange
from tests.core.network.test_handoff import pair, repositories  # noqa: F401 - fixtures

SID = "handoff-test"


def _record(worker, sid=SID):
    with worker.active(), get_db() as conn:
        return store.get_record(conn.cursor(), "sessions", sid)


def _prepare(a, b):
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (SID,))
    with a.active(), get_db() as conn:
        # The source published far more than the receiver has seen: its
        # commit record's clock is ahead of anything the receiver signs.
        store.observe_clock(conn.cursor(), 10_000)


def _route(a, b, monkeypatch, lost_commit_replies=False):
    async def call(device, action, body, **kwargs):
        if device == b.device_id:  # the source's finalize-wakes
            with b.active():
                return await control.handle(action, body, from_device=a.device_id)
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
        if lost_commit_replies and body.get("phase") == "commit":
            raise TimeoutError("the commit applied, its reply was lost")
        return result

    monkeypatch.setattr(control, "call", call)


async def _move_to_receiver(a, b, monkeypatch):
    _prepare(a, b)
    _route(a, b, monkeypatch)
    with b.active():
        await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == b.device_id


async def _delete_on_receiver(b):
    with b.active():
        await sessions_api._delete_session(database.get_session(SID), BackgroundTasks())
        assert database.get_session(SID) is None
        catalog.publish_owned()


def _assert_deleted_everywhere(a, b):
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            assert database.get_session(SID) is None, worker.name
        assert _record(worker)["deleted"], worker.name


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_arrived", [True, False])
async def test_receiver_delete_right_after_handoff_reaches_the_source(pair, monkeypatch, commit_arrived):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    if commit_arrived:
        # The source's commit record reaches the receiver before its own scan.
        exchange(a, b)
        assert _record(b)["origin_device_id"] == a.device_id

    await _delete_on_receiver(b)
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_commit_record_arriving_right_after_the_delete_commits_cannot_undo_it(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    rows = sessions_db._delete_session_rows

    def deliver_commit_after_delete(*args):
        deleted = rows(*args)
        exchange(a, b)  # the source's delayed commit record lands now
        return deleted

    monkeypatch.setattr(sessions_db, "_delete_session_rows", deliver_commit_after_delete)
    with b.active():
        await sessions_api._delete_session(database.get_session(SID), BackgroundTasks())
        assert database.get_session(SID) is None
        catalog.publish_owned()
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_after_finalize_recovers_a_lost_commit_reply(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    _prepare(a, b)
    with a.active():
        # A queued move with a pending wake: the source finalizes its wakes.
        session_moves.save(SID, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-moved", session_id=SID, prompt="later",
                              profile_id="default", fire_at=time.time() + 3600)
    _route(a, b, monkeypatch, lost_commit_replies=True)
    with b.active():
        with pytest.raises(TimeoutError):
            await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session(SID)["owner_device_id"] == b.device_id
        # The source's delivery finalizes the receiver's ownership.
        await session_handoff._finalize_moved_wakes(SID, b.device_id, release=False)
    with b.active():
        assert database.get_session(SID)["owner_device_id"] == b.device_id
    await _delete_on_receiver(b)
    _assert_deleted_everywhere(a, b)
