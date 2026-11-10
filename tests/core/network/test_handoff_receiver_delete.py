"""A chat deleted on the worker that just received it is deleted everywhere (#1308).

After a hand-off the receiver owns the chat, but the chat's sync record still
names the previous owner as its origin: the source's commit published the
ownership flip, and the receiver publishes the row itself only on its next
scan. The owner-only scan retracts only records this worker originated, so a
delete on the receiver before that scan published no tombstone, and the
previous owner kept the chat for good.

When the source's commit record reaches the receiver only after that delete,
it must not outrank the tombstone and bring the chat back on both workers:
the receiver observes the commit's clock from the commit reply.
"""
import pytest
from fastapi import BackgroundTasks

from app.api import sessions as sessions_api
from app.db import database
from app.db import network as store
from app.db.base import get_db
from app.network import catalog, control, engine, handoff
from tests.core.network.harness import converge, exchange
from tests.core.network.test_handoff import pair, repositories  # noqa: F401 - fixtures

SID = "handoff-test"


def _record(worker, sid=SID):
    with worker.active(), get_db() as conn:
        return store.get_record(conn.cursor(), "sessions", sid)


async def _move_to_receiver(a, b, monkeypatch):
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (SID,))
    with a.active(), get_db() as conn:
        # The source published far more than the receiver has seen: its
        # commit record's clock is ahead of anything the receiver signs.
        store.observe_clock(conn.cursor(), 10_000)

    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == b.device_id


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_arrived", [True, False])
async def test_receiver_delete_right_after_handoff_reaches_the_source(pair, monkeypatch, commit_arrived):
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    if commit_arrived:
        # The source's commit record reaches the receiver before its own scan.
        exchange(a, b)
        assert _record(b)["origin_device_id"] == a.device_id

    with b.active():
        await sessions_api._delete_session(database.get_session(SID), BackgroundTasks())
        assert database.get_session(SID) is None
        catalog.publish_owned()
    converge([a, b])

    for worker in (a, b):
        with worker.active():
            assert database.get_session(SID) is None, worker.name
        assert _record(worker)["deleted"], worker.name
