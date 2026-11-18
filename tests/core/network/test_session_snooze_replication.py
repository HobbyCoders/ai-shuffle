"""Snooze rides the signed shared-membership record, across mixed versions."""
import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api import sessions as api
from app.core import session_snooze
from app.db import database, network as store
from app.network import engine, policy
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session


def _soon(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


async def _patch(sid, active, snoozed_until=None):
    return await api.set_session_active(
        Request({"type": "http", "headers": []}), sid,
        api.SessionActiveUpdate(is_active=active, snoozed_until=snoozed_until), token="admin",
    )


def _old_serialize(record):
    value = record.get("is_active")
    if isinstance(value, int) and value in (0, 1):
        record["is_active"] = bool(value)
    return record


# The collection exactly as a build from before snooze registers it.
OLD = dataclasses.replace(
    policy.collection("session_active"), fields=("session_id", "is_active", "updated_at"),
    serialize_hook=_old_serialize, materialize_hook=None, late_fields=())


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app.core.sync_engine import sync_engine
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", AsyncMock())


def _snooze_of(worker, sid):
    with worker.active():
        row = database.get_session(sid)
        return bool(row["is_active"]), session_snooze.canonical(row["snoozed_until"])


def _pair(workers):
    a, b = workers("owner"), workers("mirror")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "s1", "Snoozable")
    converge([a, b])
    return a, b


async def test_snooze_set_on_mirror_while_owner_offline_reaches_owner(workers):
    a, b = _pair(workers)
    when = _soon()
    with b.active():
        store.touch_member(a.device_id, endpoints=[])
        await _patch("s1", False, when)
        ids = {s["id"] for s in database.get_sessions_cursor(limit=10, membership="snoozed")[0]}
        assert ids == {"s1"}
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (False, session_snooze.canonical(when))


async def test_owner_wakes_due_snooze_and_peers_follow(workers):
    a, b = _pair(workers)
    with b.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    # The mirror does not run the chat: it leaves the wake to the owner until
    # the grace period has passed.
    with b.active():
        assert session_snooze.due_snoozes(now=_soon(2)) == []
        assert len(session_snooze.due_snoozes(now=_soon(1) + session_snooze.PEER_GRACE + timedelta(seconds=1))) == 1
    with a.active():
        assert await session_snooze.wake_due_once(now=_soon(2)) == 1
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (True, None)


async def test_newer_resnooze_on_peer_beats_stale_wake(workers):
    a, b = _pair(workers)
    first = _soon(1)
    with a.active():
        await _patch("s1", False, first)
    converge([a, b])
    later = _soon(120)
    with b.active():
        await _patch("s1", False, later)
    converge([a, b])
    with a.active():
        assert not await session_snooze.wake("s1", expected=session_snooze.canonical(first), reason="test")
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (False, session_snooze.canonical(later))


@pytest.mark.parametrize("old_active", [True, False])
async def test_older_peer_record_without_the_field_ends_the_snooze(workers, monkeypatch, old_active):
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon())
    converge([a, b])
    # b runs an older build: it signs membership without snoozed_until.
    current = policy.collection("session_active")
    monkeypatch.setitem(policy.COLLECTIONS, "session_active", OLD)
    with b.active():
        await _patch("s1", not old_active)
        await _patch("s1", old_active)
        ops, _, _ = store.exchange_batch({}, exclude_origin=a.device_id)
    monkeypatch.setitem(policy.COLLECTIONS, "session_active", current)
    assert ops
    signed = [op for op in ops if op["collection"] == "session_active"]
    assert signed
    result = exchange(b, a)
    assert not result.rejected and not result.retry
    # An older build's settle or activate is a plain one: no snooze survives it.
    assert _snooze_of(a, "s1") == (old_active, None)


async def test_older_peer_accepts_a_snoozed_record(workers, monkeypatch):
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon())
    # b runs an older build: the extra field is ignored, the settle applies.
    monkeypatch.setitem(policy.COLLECTIONS, "session_active", OLD)
    result = exchange(a, b)
    assert not result.rejected and not result.retry
    with b.active():
        assert bool(database.get_session("s1")["is_active"]) is False


def test_unsnoozed_record_keeps_the_hash_an_older_build_stored():
    new = policy.collection("session_active")
    row = {"session_id": "s1", "is_active": False, "updated_at": "2026-01-01T00:00:00+00:00",
           "snoozed_until": None}
    plain_new = engine.serialize_row(new, dict(row))
    assert "snoozed_until" not in plain_new
    assert engine.content_hash(new, plain_new) == engine.content_hash(OLD, engine.serialize_row(OLD, dict(row)))
    snoozed = engine.serialize_row(new, {**row, "snoozed_until": "2030-01-01T09:00:00+00:00"})
    assert snoozed["snoozed_until"] == "2030-01-01T09:00:00+00:00"
    assert engine.content_hash(new, snoozed) != engine.content_hash(new, plain_new)


@pytest.mark.parametrize("value", ["tomorrow", "2030-01-01T09:00:00", 12345])
def test_malformed_wake_time_is_rejected(value):
    assert not engine._valid_snooze(value)
    assert engine._valid_snooze(None) and engine._valid_snooze("2030-01-01T09:00:00+00:00")
