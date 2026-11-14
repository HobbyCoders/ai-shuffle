"""A restored worker gets back the versions it signed after the backup (#1568).

Peers keep the ops a worker signed after its last backup. Once the restored
worker has moved to a new incarnation, an exchange serves it its own streams
from earlier incarnations, past what it holds, so it re-imports them. Its
current incarnation is never echoed back, and a worker that does not
advertise support is served exactly as before.
"""
from unittest.mock import patch

import httpx
import pytest

from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.db.base import get_db
from app.db.sessions import set_session_run_binding
from app.network import catalog, engine, transport
from tests.core.network.harness import _ACTIVE, converge, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import _network, _session
from tests.core.network.test_restored_worker_ordering import _restore
from tests.core.network.test_transport import ServedBy


def _restored(workers):
    """B wrote ``lost-0..4`` after its backup; A holds them; B is put back."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "before-backup")
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(5):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    assert owner_pref(a, "theme") == "lost-4"
    _restore(b, backup)
    assert owner_pref(b, "theme") == "before-backup"
    return a, b


def _head(worker, origin, incarnation) -> int:
    with worker.active():
        return int(store.local_watermarks().get(origin, {}).get(str(incarnation), 0))


class _Served:
    """Record every batch ``server`` hands out through ``store.exchange_batch``."""

    def __init__(self, server):
        self.server = server
        self.ops = []
        self.snapshots = 0
        self._real = store.exchange_batch

    def __call__(self, *args, **kwargs):
        ops, heads, snapshot = self._real(*args, **kwargs)
        if _ACTIVE.get() is self.server:
            self.ops.extend(ops)
            self.snapshots += bool(snapshot)
        return ops, heads, snapshot

    def streams(self, origin):
        return sorted({(op["origin_incarnation"], op["origin_seq"]) for op in self.ops
                       if op["origin_device_id"] == origin})


async def _sync(dialer, server, client, base_url, **kwargs):
    with dialer.active():
        link = transport.PeerLink(server.device_id, [base_url])
        return await link.sync_once(client, engine.context(), **kwargs)


@pytest.mark.asyncio
async def test_restored_worker_pulls_back_what_it_signed_after_the_backup(workers):
    a, b = _restored(workers)
    lost_head = _head(a, b.device_id, 1)
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            # Still on incarnation 1: its own stream is not sent back to it; the
            # pass ends by finding the rollback proof and moving to incarnation 2.
            await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        with b.active():
            assert engine.context().identity.incarnation == 2
        assert served.streams(b.device_id) == []
        assert owner_pref(b, "theme") == "before-backup"

        set_owner_pref(b, "after", "incarnation-2")
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            await _sync(b, a, client, served_by_a.base_url)
    assert owner_pref(b, "theme") == "lost-4"
    # Exactly the tail B lacked, all from incarnation 1.
    resent = served.streams(b.device_id)
    assert resent and {inc for inc, _ in resent} == {1}
    assert resent[-1] == (1, lost_head)
    with b.active(), get_db() as conn:
        assert store.own_watermark(conn.cursor(), b.device_id, 1) == lost_head
    assert owner_pref(a, "after") == "incarnation-2"

    # A holds B's incarnation 2 op now and still never serves it back to B.
    with a.active():
        assert _head(a, b.device_id, 2) >= 1
        ops = store.exchange_batch({}, exclude_origin=b.device_id, own_history_before=2)[0]
    mine = {op["origin_incarnation"] for op in ops if op["origin_device_id"] == b.device_id}
    assert mine == {1}
    # Caught up: the next exchange carries nothing of B's.
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops, _, snapshot = store.exchange_batch(marks, exclude_origin=b.device_id, own_history_before=2)
    assert not snapshot and not [op for op in ops if op["origin_device_id"] == b.device_id]
    converge([a, b])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == "lost-4"


@pytest.mark.asyncio
async def test_peer_pushes_a_restored_worker_its_earlier_incarnation(workers):
    """The other direction: A dials B and pushes B's lost tail to it."""
    a, b = _restored(workers)
    served_by_a = ServedBy(a, "https://a.example.com")
    served_by_b = ServedBy(b, "https://b.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
    with b.active():
        assert engine.context().identity.incarnation == 2
    assert owner_pref(b, "theme") == "before-backup"
    async with httpx.AsyncClient(transport=served_by_b.transport()) as client:
        await _sync(a, b, client, served_by_b.base_url)
    assert owner_pref(b, "theme") == "lost-4"


@pytest.mark.asyncio
async def test_worker_without_support_is_served_as_before(workers, monkeypatch):
    """An older worker does not advertise support: none of its streams come back."""
    a, b = _restored(workers)
    capabilities = transport._capabilities

    def older(*args, **kwargs):
        caps = dict(capabilities(*args, **kwargs))
        caps.pop(transport.OWN_HISTORY_CAPABILITY, None)
        return caps
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        with b.active():
            monkeypatch.setattr(transport, "_capabilities", older)
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            await _sync(b, a, client, served_by_a.base_url)
    assert served.streams(b.device_id) == []
    assert owner_pref(b, "theme") == "before-backup"


@pytest.mark.asyncio
async def test_compacted_gap_stops_the_replay_without_a_state_copy(workers):
    """Only the unbroken prefix comes back; nothing past a hole is claimed received."""
    a, b = _restored(workers)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    # A compacted one superseded op in the middle of what B lacks.
    a.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 AND seq = ?",
          (b.device_id, held + 3))
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        for _ in range(2):
            served = _Served(a)
            with patch.object(store, "exchange_batch", served):
                summary = await _sync(b, a, client, served_by_a.base_url)
            assert not summary.get("snapshot_required") and not served.snapshots
    assert owner_pref(b, "theme") == "lost-1"
    assert _head(b, b.device_id, 1) == held + 2
    with b.active(), get_db() as conn:
        assert store.count_pending(conn.cursor(), b.device_id, 1) == 0  # nothing past the hole sits unapplied


def _detect_restore(a, b, incarnation):
    """Hand B one op it signed past its outbox (the proof any peer can relay)."""
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, incarnation)
    with a.active(), get_db() as conn:
        proof = store.get_op(conn.cursor(), b.device_id, incarnation, held + 1)
    with b.active():
        assert engine.apply_batch([proof]).own_ahead
        assert engine.context().identity.incarnation == incarnation + 1


def _rename(worker, title):
    with worker.active():
        database.update_session("chat", title=title)
        engine.scan_all()


def test_repeated_restores_reimport_every_earlier_incarnation_in_any_order(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v0")
    _session(b, "chat", title="v0", events=0)
    converge([a, b])
    first = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-1")
    set_owner_pref(b, "one", "incarnation-1")
    _rename(b, "lost-1")
    converge([a, b])
    _restore(b, first)
    _detect_restore(a, b, 1)
    second = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-2")
    set_owner_pref(b, "two", "incarnation-2")
    _rename(b, "lost-2")
    converge([a, b])
    _restore(b, second)
    _detect_restore(a, b, 2)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops, _, snapshot = store.exchange_batch(marks, exclude_origin=b.device_id, own_history_before=3)
    mine = [op for op in ops if op["origin_device_id"] == b.device_id]
    assert not snapshot and {op["origin_incarnation"] for op in mine} == {1, 2}
    # The newer stream first: the older one is still history, not a stale stream.
    with b.active():
        for incarnation in (2, 1):
            result = engine.apply_batch([op for op in mine if op["origin_incarnation"] == incarnation])
            assert not result.rejected and not result.retry and not result.pending
    # Ordered by (clock, origin), not by incarnation: each restore moved B's
    # clock past what A proved it signed, so the later incarnation's versions
    # carry the higher clocks, for a shared setting and the chat it runs alike.
    assert (owner_pref(b, "theme"), owner_pref(b, "one"), owner_pref(b, "two"), _chat(b)[0]) == \
        ("lost-2", "incarnation-1", "incarnation-2", "lost-2")
    for incarnation in (1, 2):
        assert _head(b, b.device_id, incarnation) == _head(a, b.device_id, incarnation)


@pytest.mark.asyncio
async def test_push_stops_at_a_hole_in_the_earlier_incarnation(workers):
    """A's push sends B's old stream only up to a hole, and everything else as usual."""
    a, b = _restored(workers)
    served_by_a = ServedBy(a, "https://a.example.com")
    served_by_b = ServedBy(b, "https://b.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    a.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 AND seq = ?",
          (b.device_id, held + 1))
    set_owner_pref(a, "from-a", "pushed")
    async with httpx.AsyncClient(transport=served_by_b.transport()) as client:
        await _sync(a, b, client, served_by_b.base_url)
    assert owner_pref(b, "from-a") == "pushed"
    assert owner_pref(b, "theme") == "before-backup"
    assert _head(b, b.device_id, 1) == held


# ---------------------------------------------------------------------------
# A chat the restored worker runs (owner-ordered rows)
# ---------------------------------------------------------------------------

def _chat(worker):
    with worker.active():
        row = database.get_session("chat")
        return row["title"], row["permission_mode"], [
            e["payload"].get("content") for e in events_db.get_events("chat")]


def _turn(worker, text, *, publish=True):
    with worker.active():
        events_db.append_events("chat", [{"type": "message.user",
                                          "payload": {"type": "message.user", "content": text}}])
        if publish:
            engine.scan_all()


def _restored_chat(workers):
    """B runs a chat; after its backup it renamed it, changed its permission
    mode and took a turn. A holds all of that; B is put back."""
    a, b = _network(workers, "a", "b")
    _session(b, "chat", title="before-backup", events=1)
    converge([a, b])
    backup = b.raw.serialize()
    with b.active():
        database.update_session("chat", title="lost-title")
        set_session_run_binding("chat", permission_mode="acceptEdits")
    _turn(b, "lost turn")
    converge([a, b])
    assert _chat(a) == ("lost-title", "acceptEdits", ["hello 0 from b", "lost turn"])
    _restore(b, backup)
    return a, b


async def _pull_twice(a, b):
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        with b.active():
            assert engine.context().identity.incarnation == 2
        yield
        for _ in range(2):
            await _sync(b, a, client, served_by_a.base_url)


@pytest.mark.asyncio
async def test_restored_worker_gets_back_its_chat_title_permission_and_turns(workers):
    a, b = _restored_chat(workers)
    async for _ in _pull_twice(a, b):
        pass
    # The permission mode is the owner's own field: its own signed record restores it.
    assert _chat(b) == ("lost-title", "acceptEdits", ["hello 0 from b", "lost turn"])
    converge([a, b])
    assert _chat(a) == _chat(b)


@pytest.mark.asyncio
async def test_a_write_after_the_restore_beats_every_version_it_has_seen(workers):
    """The restore moved B's clock past the versions A proved it signed, so
    what B writes next wins on every replica; what it gets back is kept as a
    conflict, not dropped silently."""
    a, b = _restored_chat(workers)
    async for _ in _pull_twice(a, b):
        with b.active():
            database.update_session("chat", title="new-title")
            engine.scan_all()
        _turn(b, "new turn")
    assert _chat(b) == ("new-title", None, ["hello 0 from b", "new turn"])
    assert _head(b, b.device_id, 1) == _head(a, b.device_id, 1)  # all consumed, none left to retry
    with b.active():
        lost = {c["record_id"] for c in store.list_conflicts() if c["losing_device_id"] == b.device_id}
    assert "chat" in lost
    converge([a, b])
    assert _chat(a) == _chat(b) == ("new-title", None, ["hello 0 from b", "new turn"])


# ---------------------------------------------------------------------------
# Three replicas: every one picks the same winner (#1568)
# ---------------------------------------------------------------------------

def _offer(sender, receiver):
    """What ``sender`` hands ``receiver``, as ``peer_exchange`` serves a current build."""
    with receiver.active():
        marks = store.local_watermarks()
        incarnation = engine.context().identity.incarnation
    own_history = transport.own_history_before({transport.OWN_HISTORY_CAPABILITY: 1}, incarnation)
    with sender.active():
        ops, _, snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id,
                                                own_history_before=own_history)
    assert not snapshot
    return ops


def _exchange(sender, receiver):
    ops = _offer(sender, receiver)
    with receiver.active():
        return engine.apply_batch(ops)


def _converge(workers):
    pairs = [(sender, receiver) for sender in workers for receiver in workers if sender is not receiver]
    reasons = set()
    for _ in range(3):
        for sender, receiver in pairs:
            reasons |= set(_exchange(sender, receiver).reasons)
    assert not reasons
    # Settled: no pair keeps re-sending what the other already refused.
    assert {(s.name, r.name): len(_offer(s, r)) for s, r in pairs} == {(s.name, r.name): 0 for s, r in pairs}


def _streams(worker, origin):
    with worker.active():
        return store.local_watermarks().get(origin, {})


def test_a_relayed_lost_version_wins_on_every_replica(workers):
    """A holds B's first write after its backup, C the next twenty. B restores,
    takes its proof from A and writes before it reaches C: C's version has the
    higher clock, so it wins everywhere, B included, and A takes C's relay of
    B's older stream instead of refusing it as stale."""
    a, b, c = _network(workers, "a", "b", "c")
    set_owner_pref(b, "theme", "v0")
    _converge([a, b, c])
    backup = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-0")
    _exchange(b, a)
    for n in range(1, 20):
        set_owner_pref(b, "theme", f"lost-{n}")
    _exchange(b, c)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    set_owner_pref(b, "theme", "incarnation-2")
    _exchange(b, a)
    _converge([a, b, c])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == owner_pref(c, "theme") == "lost-19"
    assert _streams(a, b.device_id) == _streams(b, b.device_id) == _streams(c, b.device_id)
    with b.active():
        assert any(conflict["losing_device_id"] == b.device_id and conflict["record_id"].endswith("theme")
                   for conflict in store.list_conflicts())


def test_replicas_agree_on_a_restored_chat_whatever_order_its_versions_arrive(workers):
    """Owner-ordered rows too: B's own versions compare by clock, not by
    "the same device always wins", which depends on arrival order."""
    a, b, c = _network(workers, "a", "b", "c")
    _session(b, "chat", title="before-backup", events=1)
    _converge([a, b, c])
    backup = b.raw.serialize()
    _turn(b, "lost turn 1")
    _exchange(b, a)
    _turn(b, "lost turn 2")
    _turn(b, "lost turn 3")
    _rename(b, "lost-title")
    _exchange(b, c)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    _rename(b, "new-title")  # newer than what A proved, older than C's versions
    _exchange(b, a)
    _converge([a, b, c])
    turns = ["hello 0 from b", "lost turn 1", "lost turn 2", "lost turn 3"]
    assert _chat(a) == _chat(b) == _chat(c) == ("lost-title", None, turns)
    assert _streams(a, b.device_id) == _streams(b, b.device_id) == _streams(c, b.device_id)


# ---------------------------------------------------------------------------
# A restore fork: B's two incarnations on one record (#1568)
# ---------------------------------------------------------------------------

def _theme(worker):
    rows = worker.sql("SELECT clock, origin_device_id FROM sync_records "
                      "WHERE collection = 'user_preferences' AND record_id LIKE ?", ("%theme",))
    assert len(rows) == 1
    return int(rows[0]["clock"]), rows[0]["origin_device_id"]


def _theme_incarnation(worker):
    return worker.sql("SELECT origin_incarnation FROM sync_records "
                      "WHERE collection = 'user_preferences' AND record_id LIKE ?", ("%theme",))[0]["origin_incarnation"]


def _lost_themes(worker, origin):
    """The values of ``origin``'s versions of the theme this worker kept as conflicts."""
    with worker.active():
        return sorted(conflict["losing_record"]["value"] for conflict in store.list_conflicts()
                      if conflict["record_id"].endswith("theme") and conflict["losing_device_id"] == origin)


def _observe(worker, clock):
    with worker.active(), get_db() as conn:
        store.observe_clock(conn.cursor(), clock)


def _equal_clock_fork(workers):
    """A holds B's first write after its backup, C the second. B restores and
    takes its proof from A, so its next write carries the clock of the version
    only C holds, under the same device id: (clock, origin) ties exactly."""
    a, b, c = _network(workers, "a", "b", "c")
    set_owner_pref(b, "theme", "v0")
    _converge([a, b, c])
    backup = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-0")
    _exchange(b, a)
    set_owner_pref(b, "theme", "lost-1")
    _exchange(b, c)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    lost = _theme(c)
    _observe(b, lost[0] - 1)  # no-op unless the proof left B's clock lower
    set_owner_pref(b, "theme", "new")
    _exchange(b, a)
    assert _theme(a) == lost and owner_pref(a, "theme") == "new" and owner_pref(c, "theme") == "lost-1"
    return a, b, c


def test_an_equal_clock_across_a_restore_fork_goes_to_the_later_incarnation(workers):
    """The incarnation decides only the exact tie, the same way on every replica."""
    a, b, c = _equal_clock_fork(workers)
    _converge([a, b, c])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == owner_pref(c, "theme") == "new"
    assert _streams(a, b.device_id) == _streams(b, b.device_id) == _streams(c, b.device_id)
    assert _theme_incarnation(a) == _theme_incarnation(b) == _theme_incarnation(c) == 2
    # lost-1 lost the tie wherever it was held or arrived; lost-0, the version
    # B's write displaced on A without having seen it, is kept as well.
    assert _lost_themes(c, b.device_id) == ["lost-1"]
    assert _lost_themes(a, b.device_id) == _lost_themes(b, b.device_id) == ["lost-0", "lost-1"]


def test_a_state_copy_breaks_the_equal_clock_tie_the_same_way(workers):
    a, b, c = _equal_clock_fork(workers)
    coll = engine.policy.collection("user_preferences")

    def item(worker):
        with worker.active(), get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sync_records WHERE collection = 'user_preferences' "
                           "AND record_id LIKE %s", ("%theme",))
            return engine.snapshot_item(cursor, coll, dict(cursor.fetchone()))

    def copy(sender, receiver, *, older_build=False):
        page = item(sender)
        if older_build:
            page.pop("origin_incarnation")
        with receiver.active(), get_db() as conn:
            engine.apply_snapshot_items(conn.cursor(), coll, [page], ctx=engine.context())

    # A sender on an older build sends no incarnation: the version held stays.
    copy(a, c, older_build=True)
    assert owner_pref(c, "theme") == "lost-1"
    copy(c, a)
    assert owner_pref(a, "theme") == "new"
    # B's other incarnation: kept, not dropped (lost-0 since "new" displaced it).
    assert _lost_themes(a, b.device_id) == ["lost-0", "lost-1"]
    copy(a, c)
    assert owner_pref(c, "theme") == "new"
    assert _lost_themes(c, b.device_id) == ["lost-1"]


def test_a_transcript_copy_is_compared_with_the_same_tie_break(workers):
    from app.network import transcripts
    a, b = _network(workers, "a", "b")
    coll = engine.policy.collection("session_events")
    with a.active(), get_db() as conn:
        cursor = conn.cursor()
        store.put_record(cursor, coll.name, "s|1", clock=5, origin_device_id=b.device_id, content_hash="h",
                         deleted=False, order_seq=1, origin_incarnation=2)
        store.put_record(cursor, coll.name, "t|1", clock=5, origin_device_id=b.device_id, content_hash="h",
                         deleted=False, order_seq=1)  # stored by an older build: incarnation not known

        def newer(session_id, incarnation):
            copied = {"record_id": f"{session_id}|1", "clock": 5, "origin_device_id": b.device_id}
            if incarnation:
                copied["origin_incarnation"] = incarnation
            return transcripts._newer_than_snapshot(cursor, session_id, coll, [copied], 1)

        assert newer("s", 1) and not newer("s", 2) and not newer("s", 3)
        assert newer("s", None)  # an older build's copy does not displace a known version
        assert not newer("t", 1) and not newer("t", None)


def test_a_replay_keeps_the_post_restore_write_it_displaces(workers):
    """B's published write after the restore loses to a later lost version it
    gets back: the write is kept as a conflict, by value, like any other device's."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v0")
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(30):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    clocks = [int(row["clock"]) for row in a.sql(
        "SELECT clock FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 "
        "AND collection = 'user_preferences' AND record_id LIKE ? ORDER BY seq", (b.device_id, "%theme"))]
    assert len(clocks) == 31  # v0, then lost-0..29
    _restore(b, backup)
    _detect_restore(a, b, 1)  # proves only B's first op past the backup
    set_owner_pref(b, "theme", "new")
    written = _theme(b)[0]
    assert clocks[-1] > written
    _exchange(b, a)
    for _ in range(2):
        _exchange(a, b)
    assert owner_pref(b, "theme") == "lost-29"
    # The lost versions "new" outranked (a tie included) came back as conflicts,
    # and so did "new" itself once a later lost version displaced it.
    beaten = [f"lost-{n}" for n, clock in enumerate(clocks[1:]) if clock <= written]
    assert _lost_themes(b, b.device_id) == sorted([*beaten, "new"])
    assert _lost_themes(a, b.device_id) == ["new"]
    _converge([a, b])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == "lost-29"


def test_a_prune_between_the_prefix_check_and_the_fetch_opens_no_hole(workers, monkeypatch):
    """Retention deleting an op after the prefix was measured: only the part
    before it is sent, never the ops past the hole."""
    a, b = _restored(workers)
    _detect_restore(a, b, 1)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    measure = store._unbroken_head

    def pruned_meanwhile(cursor, origin, incarnation, after, head):
        unbroken = measure(cursor, origin, incarnation, after, head)
        cursor.execute("DELETE FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                       (origin, incarnation, held + 3))
        return unbroken

    monkeypatch.setattr(store, "_unbroken_head", pruned_meanwhile)
    sent = sorted(op["origin_seq"] for op in _offer(a, b)
                  if op["origin_device_id"] == b.device_id and op["origin_incarnation"] == 1)
    assert sent == [held + 1, held + 2]


def test_another_worker_cannot_replay_owner_fields_into_a_chat(workers):
    """Only B's own signed record sets them: a member's copy still cannot."""
    a, b = _network(workers, "a", "b")
    _session(b, "chat", title="t", events=0)
    converge([a, b])
    with b.active(), get_db() as conn:
        record = {"id": "chat", "owner_device_id": b.device_id, "permission_mode": "bypassPermissions"}
        prepared = catalog.prepare_session_mirror(conn.cursor(), record, origin_device_id=a.device_id,
                                                  device_id=b.device_id, own_replay=True)
    assert "permission_mode" not in prepared
