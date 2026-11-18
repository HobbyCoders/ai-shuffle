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


def test_a_state_copy_never_decides_the_equal_clock_tie(workers):
    """A copy is the serving member's claim, not B's signed op: on an exact
    (clock, origin) tie it is skipped, whatever the incarnations, as before
    labels existed. Each replica keeps what it holds, records nothing and
    keeps its label; B's signed ops then settle the tie the same everywhere."""
    a, b, c = _equal_clock_fork(workers)
    held = {worker.name: _theme_record(worker) for worker in (a, c)}
    for _ in range(3):
        _state_copy(a, c)
        _state_copy(c, a)
    assert {worker.name: _theme_record(worker) for worker in (a, c)} == held
    assert owner_pref(a, "theme") == "new" and owner_pref(c, "theme") == "lost-1"
    assert _lost_themes(a, b.device_id) == ["lost-0"] and _lost_themes(c, b.device_id) == []
    _converge([a, b, c])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == owner_pref(c, "theme") == "new"
    assert _lost_themes(c, b.device_id) == ["lost-1"]

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


def _unset_owner_pref(worker, key):
    with worker.active():
        from app.core import owner_auth
        database.delete_user_preference("admin", owner_auth.preference_identity(), key)
        engine.scan_all()


def test_a_replay_keeps_the_post_restore_deletion_it_displaces(workers):
    """B deletes after the restore; a later lost version it gets back revives the
    record, and B's deletion is kept as a conflict, not silently dropped."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v0")
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(30):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    _restore(b, backup)
    _detect_restore(a, b, 1)  # proves only B's first op past the backup
    _unset_owner_pref(b, "theme")
    assert owner_pref(b, "theme") is None
    tombstone = b.sql("SELECT clock, deleted, origin_incarnation FROM sync_records "
                      "WHERE collection = 'user_preferences' AND record_id LIKE ?", ("%theme",))[0]
    assert tombstone["deleted"] and tombstone["origin_incarnation"] == 2
    for _ in range(2):
        _exchange(a, b)
    assert owner_pref(b, "theme") == "lost-29"
    with b.active():
        deletions = [conflict for conflict in store.list_conflicts() if conflict["record_id"].endswith("theme")
                     and conflict["losing_record"] == {"_deleted": True}]
    assert [(c["losing_device_id"], c["losing_clock"]) for c in deletions] == \
        [(b.device_id, int(tombstone["clock"]))]
    _converge([a, b])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == "lost-29"


def _state_copy(sender, receiver, collection="user_preferences"):
    """One state-copy page of ``collection`` from ``sender``, applied on ``receiver``."""
    with sender.active(), get_db() as conn:
        page, _ = engine._snapshot_page(collection, 0, ctx=engine.context(), limit=1000, _conn=conn, keyset=True)
    with receiver.active():
        return engine.apply_snapshot_chunk(page, ctx=engine.context())


def _theme_id(worker):
    return worker.sql("SELECT record_id FROM sync_records WHERE collection = 'user_preferences' "
                      "AND record_id LIKE ?", ("%theme",))[0]["record_id"]


def _theme_item(worker):
    coll = engine.policy.collection("user_preferences")
    with worker.active(), get_db() as conn:
        cursor = conn.cursor()
        return engine.snapshot_item(cursor, coll, store.get_record(cursor, coll.name, _theme_id(worker)))


def _apply_item(worker, item):
    with worker.active(), get_db() as conn:
        return engine.apply_snapshot_items(conn.cursor(), engine.policy.collection("user_preferences"), [item],
                                           ctx=engine.context())


def _theme_record(worker):
    return worker.sql("SELECT * FROM sync_records WHERE collection = 'user_preferences' AND record_id LIKE ?",
                      ("%theme",))[0]


def test_a_copy_carries_no_incarnation(workers):
    """Only a signed op carries an incarnation: a state or transcript copy
    names none, even of a version this worker holds labelled."""
    from app.network import transcripts
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v")
    _exchange(b, a)
    assert _theme_incarnation(a) == 1
    assert "origin_incarnation" not in _theme_item(a)
    _session(a, "chat", title="t", events=2)
    with a.active(), get_db() as conn:
        items, _, _, _ = transcripts._session_items(conn.cursor(), engine.policy.collection("session_events"),
                                                    "chat", 0, 1 << 20)
    assert len(items) == 2 and not any("origin_incarnation" in item for item in items)

def test_a_copy_never_sets_or_raises_the_label(workers):
    """Reviews S17-S21: whatever incarnation a copy claims, the label held
    stays, missing or not, and a version a copy installs has none."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v")
    _exchange(b, a)
    item = _theme_item(a)
    before = _theme_record(a)
    assert _apply_item(a, {**item, "origin_incarnation": 3}) == (0, [])
    assert _theme_record(a) == before
    a.sql("UPDATE sync_records SET origin_incarnation = NULL "
          "WHERE collection = 'user_preferences' AND record_id LIKE ?", ("%theme",))
    assert _apply_item(a, {**item, "origin_incarnation": 2}) == (0, [])
    assert _theme_record(a) == {**before, "origin_incarnation": None}
    newer = {**item, "clock": int(item["clock"]) + 1, "origin_incarnation": 2,
             "record": {**item["record"], "value": "w"}}
    assert _apply_item(a, newer)[0] == 1
    assert owner_pref(a, "theme") == "w" and _theme_incarnation(a) is None

def _chat_fork(workers, *, held_project, signed_project):
    """C holds B's chat V/1 in ``held_project``. Across a restore B signs W/2
    at the same clock, the same chat in ``signed_project``, which A lacks: A's
    mirror stores W without it. Only B and C have the projects."""
    a, b, c = _network(workers, "a", "b", "c")
    for worker in (b, c):
        with worker.active():
            for project in ("p", "q"):
                database.create_project(project_id=project, name=project, description=None, path=f"/srv/{project}")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, c])
    backup = b.raw.serialize()
    _rename(b, "lost-0")
    _exchange(b, a)
    b.sql("UPDATE sessions SET title = 'v', project_id = ? WHERE id = 'chat'", (held_project,))
    with b.active():
        engine.scan_all()
    _exchange(b, c)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    v = c.sql("SELECT clock, origin_incarnation FROM sync_records WHERE collection = 'sessions' "
              "AND record_id = 'chat'")[0]
    held = c.sql("SELECT title, updated_at FROM sessions WHERE id = 'chat'")[0]
    _observe(b, int(v["clock"]) - 1)
    b.sql("UPDATE sessions SET title = ?, project_id = ?, updated_at = ? WHERE id = 'chat'",
          (held["title"], signed_project, held["updated_at"]))
    with b.active():
        engine.scan_all()
    _exchange(b, a)
    w = a.sql("SELECT clock, origin_device_id, origin_incarnation FROM sync_records "
              "WHERE collection = 'sessions' AND record_id = 'chat'")[0]
    assert (int(w["clock"]), w["origin_device_id"], w["origin_incarnation"]) == (int(v["clock"]), b.device_id, 2)
    with a.active():
        assert database.get_session("chat")["project_id"] is None  # A's mirror dropped it
    return a, b, c


def _chat_project(worker):
    with worker.active():
        return database.get_session("chat")["project_id"]


def _chat_incarnation(worker):
    return worker.sql("SELECT origin_incarnation FROM sync_records WHERE collection = 'sessions' "
                      "AND record_id = 'chat'")[0]["origin_incarnation"]


def test_a_projected_copy_does_not_raise_the_label_of_the_chat_held(workers):
    """Review S19: A's copy has the content C holds, under label 2. That says
    nothing about what B signed: C keeps label 1 and takes W/2 when it comes."""
    a, _b, c = _chat_fork(workers, held_project=None, signed_project="p")
    _state_copy(a, c, "sessions")
    assert _chat_incarnation(c) == 1
    _exchange(a, c)  # B's signed W/2, relayed
    assert _chat_project(c) == "p"


def test_a_projected_copy_does_not_replace_the_chat_held_on_a_tie(workers):
    """Review S20: C holds the chat in project Q. A's copy of B's move to P
    has no project and ties C's version: it is skipped, so Q stays until B's
    signed move arrives and puts the chat in P."""
    a, _b, c = _chat_fork(workers, held_project="q", signed_project="p")
    _state_copy(a, c, "sessions")
    assert _chat_project(c) == "q" and _chat_incarnation(c) == 1
    _exchange(a, c)
    assert _chat_project(c) == "p"


def test_a_staged_plugin_row_does_not_borrow_the_signed_label(workers):
    """Review S21: A holds B's signed plugin 3.0.0 (incarnation 2) and stages
    a local 2.0.0 the scanner has not signed yet. Its copy pairs 2.0.0 with
    B's version, which C holds as 2.0.0 from incarnation 1: C keeps label 1
    and still takes the genuine 3.0.0."""
    from app.network import plugins_sync
    plugin = "x@market"

    def install(worker, version, *, publish=True):
        with worker.active():
            plugins_sync.request_install(plugin, version=version, enabled=True)
            if publish:
                engine.scan_all(collections=["plugins_desired"])

    def held(worker):
        version = worker.sql("SELECT version FROM network_plugins_desired WHERE id = ?", (plugin,))[0]["version"]
        record = worker.sql("SELECT clock, origin_incarnation FROM sync_records "
                            "WHERE collection = 'plugins_desired' AND record_id = ?", (plugin,))[0]
        return version, int(record["clock"]), record["origin_incarnation"]

    a, b, c = _network(workers, "a", "b", "c")
    install(b, "1.0.0")
    _converge([a, b, c])
    backup = b.raw.serialize()
    install(b, "1.5.0")
    _exchange(b, a)
    install(b, "2.0.0")
    _exchange(b, c)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    clock = held(c)[1]
    _observe(b, clock - 1)
    install(b, "3.0.0")
    _exchange(b, a)
    assert held(a) == ("3.0.0", clock, 2) and held(c) == ("2.0.0", clock, 1)
    install(a, "2.0.0", publish=False)  # staged; the scanner has not signed it
    _state_copy(a, c, "plugins_desired")
    assert held(c) == ("2.0.0", clock, 1)
    _exchange(a, c)  # B's signed 3.0.0, relayed
    assert held(c) == ("3.0.0", clock, 2)

def test_a_refused_replay_keeps_no_conflict_for_the_deletion_it_could_not_undo(workers):
    """Review S16: B restores and deletes its chat; its own later rename from
    before the restore comes back. The chat stays deleted (#1308), so the
    deletion did not lose and no conflict says it did."""
    a, b = _network(workers, "a", "b")
    _session(b, "chat", title="t", events=0)
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(5):
        _rename(b, f"lost-{n}")
    _exchange(b, a)
    _restore(b, backup)
    _detect_restore(a, b, 1)  # proves only B's first op past the backup
    with b.active():
        assert database.delete_session("chat")
        engine.publish_session_deletions()
    tombstone = b.sql("SELECT deleted, origin_incarnation FROM sync_records "
                      "WHERE collection = 'sessions' AND record_id = 'chat'")[0]
    assert tombstone["deleted"] and tombstone["origin_incarnation"] == 2
    for _ in range(2):
        _exchange(a, b)
    with b.active():
        assert database.get_session("chat") is None
        # B's renames that lost to the deletion are kept; the deletion is no loser.
        assert [conflict for conflict in store.list_conflicts() if conflict["record_id"] == "chat"
                and conflict["losing_record"] == {"_deleted": True}] == []


# ---------------------------------------------------------------------------
# No restore on either side: the outcome is main's (review S22)
# ---------------------------------------------------------------------------

def test_a_signed_chat_update_repairs_an_earlier_projected_copy(workers):
    """Review S22, no restore: B links its chat to P, which A lacks, so A's
    copy has no project. C commits that copy before B's op: the op is B's own
    stream moving on and applies, as on main, so C ends in P."""
    a, b, c = _network(workers, "a", "b", "c")
    for worker in (b, c):
        with worker.active():
            database.create_project(project_id="p", name="p", description=None, path="/srv/p")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, c])
    b.sql("UPDATE sessions SET project_id = 'p' WHERE id = 'chat'")
    with b.active():
        engine.scan_all()
    _exchange(b, a)
    assert _chat_project(a) is None  # A's mirror dropped it
    _state_copy(a, c, "sessions")
    assert _chat_project(c) is None and _chat_incarnation(c) is None
    _exchange(a, c)  # B's signed op, relayed
    assert _chat_project(c) == "p"



class _Aborted(Exception):
    pass


def _copy_overtaken_by_a_relay(workers, monkeypatch, *, restore=True, tie=False, moment="after"):
    """A holds B's first rename after its backup, D the first two. B restores
    and writes "new" past the first, at the clock of the second (``tie``) or
    past both, and A takes it. C applies A's state copy, one page per commit,
    and D relays B's second rename, which A never had: after the copy, between
    its pages, or after the copy stopped past the chat page, before C resumes
    it from the pages it saved (``moment``). Without ``restore`` B writes
    nothing more: the rename is its next op. Checks that the copy's claims
    reach no stream state of C while it runs, nor after it stopped (S26)."""
    from app.network import peer_snapshot
    a, b, c, d = _network(workers, "a", "b", "c", "d")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, c, d])
    backup = b.raw.serialize()
    _rename(b, "lost-0")
    _exchange(b, a)
    _rename(b, "lost-1")
    _exchange(b, d)
    expected = "lost-0"
    if restore:
        _restore(b, backup)
        _detect_restore(a, b, 1)
        lost = _chat_clock(d)
        _observe(b, lost - 1 if tie else lost)
        _rename(b, "new")
        _exchange(b, a)
        assert set(_streams(a, b.device_id)) == {"1", "2"}
        assert (_chat_clock(a) == lost) == tie
        expected = "new"
    assert _sweep_value(a, "sessions") == expected
    before = dict(_streams(c, b.device_id))
    with a.active():
        manifest = peer_snapshot.capture(c.device_id)
        raw = [peer_snapshot.page(c.device_id, manifest["transfer"], i) for i in range(len(manifest["pages"]))]
    directory = c.data_dir / "copy"
    directory.mkdir()
    for index, page in enumerate(raw):
        (directory / f"{index}.json").write_bytes(page)
    sessions = max(i for i, page in enumerate(manifest["pages"]) if page["collection"] == "sessions")
    assert sessions + 1 < len(manifest["pages"])
    monkeypatch.setattr(peer_snapshot, "APPLY_BATCH_ITEMS", 1)  # commit after every page
    save = peer_snapshot._save_applied
    relayed = []

    def committed(directory, manifest, pages):
        save(directory, manifest, pages)
        if moment != "after" and not relayed and sessions < pages < len(manifest["pages"]):
            # The chat page is in, the frontier is not.
            assert _sweep_value(c, "sessions") == expected
            assert _streams(c, b.device_id) == before and _cutoff(c, b) == _cutoff_of(before)
            relayed.append(True)
            if moment == "aborted":
                raise _Aborted
            _exchange(d, c)

    monkeypatch.setattr(peer_snapshot, "_save_applied", committed)
    with c.active():
        if moment == "aborted":
            with pytest.raises(_Aborted):
                peer_snapshot.apply(manifest, directory, a.device_id)
        else:
            peer_snapshot.apply(manifest, directory, a.device_id)
    assert bool(relayed) == (moment != "after")
    if moment == "aborted":
        # Nothing the copy claimed reaches C's streams of B; its rows stand.
        assert _streams(c, b.device_id) == before and _cutoff(c, b) == _cutoff_of(before)
        assert _sweep_value(c, "sessions") == expected
        _exchange(d, c)  # the rename, relayed while the copy is stopped
        assert _streams(c, b.device_id)["1"] == _streams(d, b.device_id)["1"]
        with c.active():  # resumed: past the saved pages, then the frontier
            peer_snapshot.apply(manifest, directory, a.device_id)
    if restore:
        assert _streams(c, b.device_id)["2"] == _streams(a, b.device_id)["2"]
    if moment == "after":
        # D still has the second rename for C: it is the next op, not a duplicate.
        assert int(_streams(d, b.device_id)["1"]) == int(_streams(c, b.device_id)["1"]) + 1
        _exchange(d, c)
    assert _streams(c, b.device_id)["1"] == _streams(d, b.device_id)["1"]
    return a, b, c, d


def _chat_clock(worker):
    return int(worker.sql("SELECT clock FROM sync_records WHERE collection = 'sessions' "
                          "AND record_id = 'chat'")[0]["clock"])


def _cutoff_of(streams):
    """The stream position a revocation of the device would sign, from these streams."""
    incarnation = max((int(i) for i in streams), default=0)
    return incarnation, int(streams.get(str(incarnation), 0))


def _cutoff(worker, device):
    """What ``worker`` would sign today as ``device``'s revocation cutoff."""
    with worker.active(), get_db() as conn:
        cursor = conn.cursor()
        incarnation = store.highest_incarnation(cursor, device.device_id)
        return incarnation, store.get_watermark(cursor, device.device_id, incarnation) if incarnation else 0


@pytest.mark.parametrize("moment", ["after", "between", "aborted"])
@pytest.mark.parametrize("restore,tie", [(False, False), (True, False), (True, True)],
                         ids=["no-restore", "later-clock", "same-clock"])
def test_an_older_incarnation_op_never_replaces_a_copied_write_it_cannot_be_matched_to(
        workers, monkeypatch, restore, tie, moment):
    """Reviews S22b, S24, S25, S26 and S28 together: the copy's lifecycle.
    C takes A's copy of B's chat and D relays B's pre-restore rename A never
    had: after the copy, between its pages, or while it is stopped past the
    chat page, before C resumes it and adopts its frontier. Without a restore
    the rename is B's next op and applies, as on main. After one, C holds B's
    post-restore "new" from the copy with no label, and the rename has a lower
    clock or the same one: the row keeps the incarnation its copy named, so
    the rename never replaces it, before the frontier, after it, or across
    the stop and the resume that skips the chat page. Nothing the copy claims
    reaches C's streams, so a revocation C signs names only what C received."""
    a, b, c, d = _copy_overtaken_by_a_relay(workers, monkeypatch, restore=restore, tie=tie, moment=moment)
    assert _sweep_value(c, "sessions") == ("new" if restore else "lost-1")
    _converge([a, b, c, d])
    assert {_sweep_value(w, "sessions") for w in (a, b, c, d)} == {"new" if restore else "lost-1"}
    assert all(int(seq) > 0 for seq in _streams(c, b.device_id).values())


def test_a_copy_orders_only_the_rows_it_installed(workers, monkeypatch):
    """Review S28: C holds a copy of B's chat that has no project (A lacks P)
    and was installed without a frontier, so B's signed link to P is still
    owed. B restores and writes something else, so A's next copy names B's
    incarnation 2. D relays the link while C verifies that copy, before any
    page is in: no row the copy installed is involved, so the link applies,
    as on main, and the copy's own version of the chat, a tie, leaves it."""
    from app.network import peer_snapshot
    a, b, c, d = _network(workers, "a", "b", "c", "d")
    for worker in (b, c):
        with worker.active():
            database.create_project(project_id="p", name="p", description=None, path="/srv/p")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, c, d])
    backup = b.raw.serialize()
    b.sql("UPDATE sessions SET project_id = 'p' WHERE id = 'chat'")
    with b.active():
        engine.scan_all()
    _exchange(b, a)
    _exchange(b, d)
    _state_copy(a, c, "sessions")
    assert _chat_project(c) is None and _chat_incarnation(c) is None
    _restore(b, backup)
    _detect_restore(a, b, 1)
    set_owner_pref(b, "gap", "x")
    _exchange(b, a)
    with a.active():
        manifest = peer_snapshot.capture(c.device_id)
        raw = [peer_snapshot.page(c.device_id, manifest["transfer"], i) for i in range(len(manifest["pages"]))]
    assert manifest["watermarks"][b.device_id].get("2", 0) > 0
    directory = c.data_dir / "copy"
    directory.mkdir()
    for index, page in enumerate(raw):
        (directory / f"{index}.json").write_bytes(page)
    read = peer_snapshot._read_page
    relayed = []

    def preflight(directory, index, expected):
        if not relayed:
            relayed.append(True)
            _exchange(d, c)  # B's signed link, relayed before any page is in
            assert _chat_project(c) == "p" and _chat_incarnation(c) == 1
        return read(directory, index, expected)

    monkeypatch.setattr(peer_snapshot, "_read_page", preflight)
    with c.active():
        peer_snapshot.apply(manifest, directory, a.device_id)
    assert relayed and _chat_project(c) == "p" and owner_pref(c, "gap") == "x"
    assert _streams(c, b.device_id)["1"] == _streams(d, b.device_id)["1"]


@pytest.mark.parametrize("pruned", [False, True], ids=["logged", "pruned"])
def test_a_restored_worker_keeps_its_own_unlabelled_write_at_the_replayed_clock(workers, pruned):
    """Reviews S25 and S27 on the restored worker itself: B restored before it
    had this build, so its write "new" after the restore has no label, and it
    took the clock of a rename it lost. The replayed rename is from B's older
    incarnation and only ties it, so B keeps "new", as every replica does,
    also once retention dropped the signed op of "new" from B's log."""
    a, b, d = _network(workers, "a", "b", "d")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, d])
    backup = b.raw.serialize()
    _rename(b, "lost-0")
    _exchange(b, a)
    _rename(b, "lost-1")
    _exchange(b, d)
    _restore(b, backup)
    _detect_restore(a, b, 1)
    _observe(b, _chat_clock(d) - 1)
    _rename(b, "new")
    assert _chat_clock(b) == _chat_clock(d)
    b.sql("UPDATE sync_records SET origin_incarnation = NULL WHERE collection = 'sessions' AND record_id = 'chat'")
    _exchange(b, a)
    if pruned:
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 2", (b.device_id,))
    _exchange(d, b)
    assert _sweep_value(b, "sessions") == "new"
    _converge([a, b, d])
    assert {_sweep_value(w, "sessions") for w in (a, b, d)} == {"new"}


@pytest.mark.asyncio
async def test_a_copy_of_a_lost_chat_update_blocks_its_signed_replay(workers):
    """Review S23, S27, a known limit (#1792): B changes its chat's permission
    mode after its backup, then restores and moves to incarnation 2 without
    touching the chat. A gap in D's stream makes A hand B a state copy,
    applied before the ops of the same exchange: it installs that very
    version without the owner's own fields. B's signed op for it ties the
    copy, and nothing proves the copy is that write rather than a later one
    at the same clock (a pruned log does not), so B keeps the copy: the mode
    stays as backed up until B sets it again."""
    from app.network import peer_snapshot
    a, b, d = _network(workers, "a", "b", "d")
    _session(b, "chat", title="t", events=0)
    _converge([a, b, d])
    backup = b.raw.serialize()
    with b.active():
        set_session_run_binding("chat", permission_mode="acceptEdits")
        engine.scan_all()
    _exchange(b, a)
    _restore(b, backup)
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        with b.active():
            assert engine.context().identity.incarnation == 2
        assert _chat(b)[1] is None
        set_owner_pref(d, "gap", "x")
        _exchange(d, a)
        a.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (d.device_id,))
        with a.active():
            peer_snapshot.capture(b.device_id)
        result = await _sync(b, a, client, served_by_a.base_url)
        assert result["snapshot_required"] and owner_pref(b, "gap") == "x"
        await _sync(b, a, client, served_by_a.base_url)
    assert _chat(b)[1] is None
    assert _head(b, b.device_id, 1) == _head(a, b.device_id, 1)


def _sweep_value(worker, collection):
    if collection == "sessions":
        with worker.active():
            return database.get_session("chat")["title"]
    return owner_pref(worker, "theme")


def _sweep_record(worker, collection):
    record_id = "chat" if collection == "sessions" else _theme_id(worker)
    return record_id, worker.sql("SELECT * FROM sync_records WHERE collection = ? AND record_id = ?",
                                 (collection, record_id))[0]


@pytest.mark.parametrize("collection", ["sessions", "user_preferences"])
@pytest.mark.parametrize("copy_first", [True, False], ids=["copy-first", "op-first"])
@pytest.mark.parametrize("labelled", [True, False], ids=["labelled", "unlabelled"])
@pytest.mark.parametrize("same_origin", [True, False], ids=["same-origin", "other-origin"])
@pytest.mark.parametrize("copy_ahead", [0, 1], ids=["tie", "copy-ahead"])
def test_without_a_restore_the_outcome_is_mains(workers, collection, copy_first, labelled, same_origin, copy_ahead):
    """With no restore fork on either side, the outcome equals main's. B signs
    "op" at clock K; a copy says "copy" at K (an exact tie) or K+1, from B or
    from A. The held version is labelled with B's only incarnation, or has no
    label (a legacy row, or one a copy installed). Main's rules: an owner-
    ordered op of the held version's origin applies; any other op, and every
    copy, needs a higher (clock, origin) to win; a loser of another origin, or
    a losing op, is kept as a conflict. The sweep passes on main's engine."""
    a, b, c = _network(workers, "a", "b", "c")
    if collection == "sessions":
        _session(b, "chat", title="v0", events=0)
    else:
        set_owner_pref(b, "theme", "v0")
    _converge([a, b, c])
    if collection == "sessions":
        _rename(b, "op")
    else:
        set_owner_pref(b, "theme", "op")
    coll = engine.policy.collection(collection)
    with b.active(), get_db() as conn:
        cursor = conn.cursor()
        record_id, _ = _sweep_record(b, collection)
        item = engine.snapshot_item(cursor, coll, store.get_record(cursor, collection, record_id))
    key = "title" if collection == "sessions" else "value"
    origin = b if same_origin else a
    k = int(item["clock"])
    item = {**item, "clock": k + copy_ahead, "origin_device_id": origin.device_id,
            "record": {**item["record"], key: "copy"}}

    def copy():
        with c.active(), get_db() as conn:
            engine.apply_snapshot_items(conn.cursor(), coll, [item], ctx=engine.context())

    def label():
        c.sql("UPDATE sync_records SET origin_incarnation = ? WHERE collection = ? AND record_id = ?",
              (1 if labelled else None, collection, record_id))

    if copy_first:
        copy()
        label()
        _exchange(b, c)
    else:
        _exchange(b, c)
        label()
        copy()

    ahead = not copy_ahead and b.device_id > origin.device_id  # (K, B) outranks the copy
    if copy_first:
        op_wins = ahead or (same_origin and coll.mode == engine.policy.MODE_OWNER_ORDERED)
        conflicts = 1 if (not same_origin or not op_wins) else 0
        expected = "op" if op_wins else "copy"
    else:
        conflicts = 0 if same_origin else 1
        expected = "op" if ahead or (same_origin and not copy_ahead) else "copy"
    assert _sweep_value(c, collection) == expected
    with c.active():
        # Losers from K on: a copy from A also displaces v0 first, as on main.
        assert len([conflict for conflict in store.list_conflicts() if conflict["record_id"] == record_id
                    and conflict["losing_clock"] >= k]) == conflicts
