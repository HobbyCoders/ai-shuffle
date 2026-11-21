"""#1605 review round: reproductions of the paths a code review traced.

Each test drives the real producing path on two workers and checks the end
state on BOTH: the final value, the recorded conflict, and that each
worker's watermark of the other's stream reaches that stream's head.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db import database
from app.db import network as store
from app.network import engine
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_unscanned_local_edit import own_data_keys  # noqa: F401 - fixture


def _heads_reached(a, b):
    """Each worker holds the other's whole stream (no op waits for good)."""
    for holder, origin in ((a, b), (b, a)):
        with origin.active(), database.get_db() as conn:
            head = store.own_watermark(conn.cursor(), origin.device_id, origin.identity.incarnation)
        with holder.active(), database.get_db() as conn:
            cursor = conn.cursor()
            assert store.get_watermark(cursor, origin.device_id, origin.identity.incarnation) == head, (
                holder.name, origin.name)
            assert store.count_pending(cursor, origin.device_id, origin.identity.incarnation) == 0


def _settle(a, b):
    for _ in range(3):
        converge([a, b])
        for worker in (a, b):
            with worker.active():
                engine.resume_pending()
                engine.scan_all()
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            assert engine.scan_all() == 0


def _conflicts(worker, collection, record_id):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == collection and c["record_id"] == record_id]


# -- S1: a state copy must not replace a session membership edit the outbox
# -- held back (``network_session_active.local_pending``).

def _membership(worker, session_id):
    row = worker.sql("SELECT is_active, snoozed_until, local_pending FROM network_session_active "
                     "WHERE session_id = ?", (session_id,))[0]
    return bool(row["is_active"]), row["snoozed_until"], bool(row["local_pending"])


@pytest.mark.parametrize("held_at_copy", [False, True])
def test_a_state_copy_keeps_a_held_snooze(workers, monkeypatch, held_at_copy):
    from app.core import session_snooze
    from app.network import session_active
    from tests.core.network.test_control import _new_session
    monkeypatch.setattr(engine, "kick", lambda: None)
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "chat", "Chat")
    with a.active():
        session_active.write_shared("chat", False)  # B's last signed membership comes from A
    converge([a, b])
    wake = session_snooze.canonical(datetime.now(timezone.utc) + timedelta(days=1))
    with b.active():
        engine.hold_outbox(True)
        try:
            session_active.write_shared("chat", False, wake)  # the snooze is not signed
            assert _membership(b, "chat") == (False, wake, True)
        finally:
            engine.hold_outbox(False)
    with a.active():
        session_active.write_shared("chat", True)  # newer than B's last signed version
        chunk = engine.snapshot_chunk("session_active", 0)
    with b.active():
        if held_at_copy:
            engine.hold_outbox(True)
            try:
                with pytest.raises(engine.TransientApplyError):
                    engine.apply_snapshot_chunk(chunk)  # waits: nothing can be signed
                assert _membership(b, "chat") == (False, wake, True)
            finally:
                engine.hold_outbox(False)
        engine.apply_snapshot_chunk(chunk)
        assert _membership(b, "chat")[:2] == (False, wake)
    _settle(a, b)
    for worker in (a, b):
        assert _membership(worker, "chat") == (False, wake, False), worker.name
        with worker.active():
            assert session_snooze.canonical(database.get_session("chat")["snoozed_until"]) == wake
        # A's newer activation lost to the snooze B signed over it: kept on both.
        losers = [(c["losing_device_id"], c["losing_record"].get("is_active")) for c in
                  _conflicts(worker, "session_active", "chat")]
        assert (a.device_id, True) in losers, (worker.name, losers)
    _heads_reached(a, b)


# -- S5: the upgrade backfill of quorum-free baselines must run once
# -- credential storage is unlocked, before any loop publishes or applies.

def test_a_cold_upgrade_backfills_baselines_after_credentials_unlock(workers, own_data_keys, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from unittest.mock import patch as patch_object

    from app.core import encryption, managed_connection
    from app.network import enrollment, hub, memory_records, startup, transport
    from tests.core.network.test_unscanned_local_edit import _busy, _nested_pair
    a, b, collection, rid, edit, read = _nested_pair(workers, "connector", "a")

    def baseline():
        return b.sql("SELECT quorum_free_hash AS q FROM sync_records WHERE collection = ? AND record_id = ?",
                     (collection, rid))[0]["q"]

    with b.active(), database.get_db() as conn:  # a database from before the upgrade
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sync_ops WHERE collection = %s AND record_id = %s", (collection, rid))
        cursor.execute("UPDATE sync_records SET quorum_free_hash = NULL WHERE collection = %s AND record_id = %s",
                       (collection, rid))
    engine._QUORUM_FREE.clear()  # a new process
    seen = []
    monkeypatch.setattr(transport, "start", Mock(side_effect=lambda: seen.append(baseline()) or []))
    monkeypatch.setattr(transport, "stop", AsyncMock())
    monkeypatch.setattr(hub, "start", Mock(return_value=[]))
    monkeypatch.setattr(hub, "stop", AsyncMock())
    monkeypatch.setattr(enrollment, "resume_enrollment", Mock())
    monkeypatch.setattr(managed_connection, "supervise", AsyncMock())
    monkeypatch.setattr(memory_records, "reconcile_files", Mock(return_value=0))
    monkeypatch.setattr(startup, "_tasks", [])
    with b.active():
        # main.lifespan: network initialize, THEN credential storage, THEN the loops.
        with patch_object.object(encryption, "_encryption_key", None):
            assert startup.initialize() is not None
        import asyncio

        async def loops():
            await startup.start()
            await startup.stop()
        asyncio.run(loops())
        assert seen and seen[0], "no baseline when the first loop started"
    with a.active():
        engine.publish_presence(version="9.9.9")  # the quorum moves
    assert not exchange(a, b).retry
    with a.active():
        edit(rid, "signed on a")
        engine.scan_all()
    _busy(b)  # B's clock is ahead: a stale signature from B would win
    engine._QUORUM_FREE.clear()  # B restarted before its scanner re-keyed the row
    assert not exchange(a, b).retry
    _settle(a, b)
    for worker in (a, b):
        with worker.active():
            assert read(rid) == "signed on a", worker.name
        # A's edit lost to nothing: B signed no stale copy above it.
        assert a.device_id not in [c["losing_device_id"] for c in _conflicts(worker, collection, rid)], worker.name
    _heads_reached(a, b)


# -- S2: B signs an unscanned CASCADE child just before a peer's parent
# -- deletion removes it. The deleting peer must not be handed an upsert it
# -- can never store, and the losing child must be kept as a conflict.

@pytest.mark.parametrize("kind", ["notes", "agents", "api_users"])
def test_a_child_signed_under_a_peer_parent_deletion_stalls_no_stream(workers, kind):
    from tests.core.network.test_unscanned_local_edit import _cascade_case, _pair
    parent, child, drop = _cascade_case(kind)
    a, b = _pair(workers)
    parent(a)
    with a.active():
        engine.scan_all()
    converge([a, b])
    collection = child(b)  # the scanner has not run on B
    drop(a)
    with a.active():
        engine.scan_all()
    exchange(a, b)  # B signs its child, then the cascade removes it
    with b.active():
        database.create_tag("after-b", "after-b")  # later ops on B's stream
        engine.scan_all()
    with a.active():
        database.create_tag("after-a", "after-a")
        engine.scan_all()
    _settle(a, b)
    for worker in (a, b):
        assert worker.sql(f"SELECT COUNT(*) AS n FROM {engine.policy.COLLECTIONS[collection].table}")[0]["n"] == 0
        assert worker.sql("SELECT COUNT(*) AS n FROM tags WHERE id IN ('after-a', 'after-b')")[0]["n"] == 2
    # Notes, keys: B signed its child and the deleting worker keeps it
    # (``_deleted_parent``). Agents, whose ids are reused: B signs nothing and
    # keeps its child itself, so A is handed no upsert it cannot store.
    keeper = b if kind == "agents" else a
    assert [c for c in _conflicts_in(keeper, collection) if c["losing_device_id"] == b.device_id]
    if kind == "agents":
        assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = 'permission_rules'")[0]["n"] == 0
    _heads_reached(a, b)


def _conflicts_in(worker, collection):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == collection]


# -- S4: B holds a secret-bearing connector by state copy only (persisted
# -- baseline, no retained op). A's presence moves the quorum, then A's key
# -- tombstone clears the connector's key. B edited nothing: A's next rename
# -- must win on both workers.

@pytest.mark.parametrize("scans", ["between", "once"])
@pytest.mark.parametrize("delivery", ["one batch", "presence first"])
def test_a_cleared_key_after_quorum_drift_is_not_a_local_edit(workers, own_data_keys, delivery, scans):
    """``scans``: A's scanner runs between the key deletion and the rename
    (A republishes the cleared connector first), or once after both (one
    connector op carries the cleared key and the new name)."""
    from app.db import api_users
    from tests.core.network.test_unscanned_local_edit import _busy, _nested_pair
    a, b, collection, rid, edit, read = _nested_pair(workers, "connector", "a")
    with a.active():
        api_users.create_api_user("u1", "Key", "hash")
        with database.get_db() as conn:
            conn.cursor().execute("UPDATE connectors SET api_user_id = %s WHERE id = %s", ("u1", rid))
        engine.scan_all()
    _settle(a, b)
    with b.active(), database.get_db() as conn:  # B took this version by state copy: no op kept
        conn.cursor().execute("DELETE FROM sync_ops WHERE collection = %s AND record_id = %s", (collection, rid))
    assert b.sql("SELECT quorum_free_hash AS q FROM sync_records WHERE collection = ? AND record_id = ?",
                 (collection, rid))[0]["q"]
    engine._QUORUM_FREE.clear()  # B restarted
    with a.active():
        engine.publish_presence(version="9.9.9")  # the quorum moves
    if delivery == "presence first":
        assert not exchange(a, b).retry
    with a.active():
        api_users.delete_api_user("u1")
        if scans == "between":
            engine.scan_all()
        edit(rid, "renamed on a")
        engine.scan_all()
    _busy(b)  # B's clock is ahead: a stale signature from B would win
    signed = b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = ? AND origin_device_id = ?",
                   (collection, b.device_id))[0]["n"]
    assert not exchange(a, b).retry
    assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = ? AND origin_device_id = ?",
                 (collection, b.device_id))[0]["n"] == signed  # B signed no stale copy
    _settle(a, b)
    for worker in (a, b):
        with worker.active():
            assert read(rid) == "renamed on a", worker.name
            assert database.get_connector(rid)["api_user_id"] is None
        assert a.device_id not in [c["losing_device_id"] for c in _conflicts(worker, collection, rid)], worker.name
    _heads_reached(a, b)


def test_a_real_edit_across_a_cleared_key_after_quorum_drift_is_kept(workers, own_data_keys):
    """The S4 baseline move must not hide a real unscanned edit (#1605)."""
    from app.db import api_users
    from tests.core.network.test_unscanned_local_edit import _nested_pair
    a, b, collection, rid, edit, read = _nested_pair(workers, "connector", "a")
    with a.active():
        api_users.create_api_user("u1", "Key", "hash")
        with database.get_db() as conn:
            conn.cursor().execute("UPDATE connectors SET api_user_id = %s WHERE id = %s", ("u1", rid))
        engine.scan_all()
    _settle(a, b)
    with b.active(), database.get_db() as conn:
        conn.cursor().execute("DELETE FROM sync_ops WHERE collection = %s AND record_id = %s", (collection, rid))
    engine._QUORUM_FREE.clear()
    with a.active():
        engine.publish_presence(version="9.9.9")
        api_users.delete_api_user("u1")
        edit(rid, "renamed on a")
        engine.scan_all()
    with b.active():
        edit(rid, "edited on b")  # the scanner has not run on B
    signed = b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = ? AND origin_device_id = ?",
                   (collection, b.device_id))[0]["n"]
    assert not exchange(a, b).retry
    assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = ? AND origin_device_id = ?",
                 (collection, b.device_id))[0]["n"] == signed + 1  # B's edit was signed first
    _settle(a, b)
    with a.active():
        final = read(rid)
    assert final in ("renamed on a", "edited on b")
    loser = "edited on b" if final == "renamed on a" else "renamed on a"
    for worker in (a, b):
        with worker.active():
            assert read(rid) == final, worker.name
        assert loser in [c["losing_record"].get("name") for c in _conflicts(worker, collection, rid)], worker.name
    _heads_reached(a, b)


# -- S3: A restarted with an unpublished template edit (outbox held). A can
# -- dial B, B cannot dial A, and B pruned ops A needs, so A must take a state
# -- copy holding a newer version of that template.

async def test_a_required_state_copy_does_not_keep_a_held_outbox_closed(workers):
    import httpx

    from app.db import templates
    from app.network import transport
    from tests.core.network.test_transport import ServedBy
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        templates.create_template("t", "original", "prompt")
        engine.scan_all()
    join(a, b)
    converge([a, b])
    with b.active():
        templates.update_template("t", name="newer on b")
        database.create_tag("pruned", "pruned")
        engine.scan_all()
        # B pruned an op A lacks: A's next exchange asks for a state copy.
        victim = next(op for op in reversed(store.ops_since({})) if op["collection"] == "tags")
        b.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    with a.active():
        store.touch_member(b.device_id, endpoints=["http://b.local:8000"])
        engine.hold_outbox(True)  # A restarted
        templates.update_template("t", name="edited on a")  # not published
    served = ServedBy(b, "http://b.local:8000")
    outcomes = []
    with a.active():
        link = transport.PeerLink(b.device_id, [served.base_url])
        async with httpx.AsyncClient(transport=served.transport()) as client:
            for _ in range(3):  # three polls
                try:
                    outcomes.append(await link.sync_once(client, engine.require_context()))
                except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
                    outcomes.append(repr(exc))
        held = engine.outbox_held()
    try:
        assert not held, outcomes
        _settle(a, b)
        with a.active():
            final = templates.get_template("t")["name"]
        assert final in ("edited on a", "newer on b")
        loser = "newer on b" if final == "edited on a" else "edited on a"
        for worker in (a, b):
            with worker.active():
                assert templates.get_template("t")["name"] == final, worker.name
            assert loser in [c["losing_record"].get("name") for c in _conflicts(worker, "templates", "t")], worker.name
        _heads_reached(a, b)
    finally:
        with a.active():
            engine.hold_outbox(False)


# -- S7: B restarted (outbox held) with an unpublished permission rule for an
# -- agent A deleted, and A pruned ops B needs, so B takes a state copy holding
# -- the agent's tombstone. A scanner pass during the download must sign
# -- nothing: the copy keeps the rule as B's conflict, as S2's apply does.

async def test_a_scan_during_a_held_state_copy_signs_no_rule_the_copy_deletes(workers, monkeypatch):
    import httpx

    from app.db import profiles, subagents
    from app.network import transport
    from tests.core.network.test_transport import ServedBy
    from tests.core.network.test_unscanned_local_edit import _pair
    a, b = _pair(workers)
    with a.active():
        database.create_profile(profile_id="p1", name="Agent", description=None, config={})
        engine.scan_all()
    converge([a, b])
    with a.active():
        database.create_tag("pruned", "pruned")
        engine.scan_all()
        profiles.delete_profile("p1")
        engine.scan_all()
        # A pruned an op B lacks below its head: B's next exchange asks for a
        # state copy, which carries the agent's tombstone.
        victim = next(op for op in reversed(store.ops_since({})) if op["collection"] == "tags")
        assert victim["record_id"] == "pruned"
        a.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        engine.hold_outbox(True)  # B restarted
        rule = subagents.add_permission_rule("p1", "Bash", "ls *", "allow")["id"]  # not published
        database.create_tag("after-b", "after-b")  # a later edit on B's stream
    scans = []
    download = transport.PeerLink._download_state

    async def scan_first(self, client, ctx, manifest, **kwargs):
        scans.append(await asyncio.to_thread(engine.scan_all))  # the scanner wakes mid-copy
        return await download(self, client, ctx, manifest, **kwargs)

    monkeypatch.setattr(transport.PeerLink, "_download_state", scan_first)
    served = ServedBy(a, "http://a.local:8000")
    outcomes = []
    try:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            async with httpx.AsyncClient(transport=served.transport()) as client:
                for _ in range(2):  # two polls
                    try:
                        outcomes.append(await link.sync_once(client, engine.require_context()))
                    except Exception as exc:  # noqa: BLE001 - recorded for the assertions below
                        outcomes.append(repr(exc))
            held = engine.outbox_held()
        assert scans == [0], outcomes  # nothing was signed while the copy was pending
        assert not held, outcomes  # the applied copy opened the outbox
        _settle(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    for worker in (a, b):
        assert worker.sql("SELECT COUNT(*) AS n FROM permission_rules")[0]["n"] == 0, worker.name
        assert worker.sql("SELECT COUNT(*) AS n FROM agents WHERE id = 'p1'")[0]["n"] == 0, worker.name
        assert worker.sql("SELECT COUNT(*) AS n FROM tags WHERE id = 'after-b'")[0]["n"] == 1, worker.name
    # B signed nothing for the rule and keeps it itself; A was handed no upsert.
    assert rule in [c["record_id"] for c in _conflicts_in(b, "permission_rules")
                    if c["losing_device_id"] == b.device_id]
    assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = 'permission_rules'")[0]["n"] == 0
    _heads_reached(a, b)


# -- S9: the S7 setup, but A cannot hand out the copy yet (it is still
# -- receiving its own) during B's launch catch-up. No watermark moves; the
# -- catch-up must not open the hold, or the scanner signs the rule the copy
# -- deletes on the next pass.

async def test_a_deferred_state_copy_keeps_a_held_outbox_closed_during_launch_catch_up(workers, monkeypatch):
    import httpx

    from app.db import profiles, subagents
    from app.network import peer_snapshot, transport
    from tests.core.network.test_transport import ServedBy
    from tests.core.network.test_unscanned_local_edit import _pair
    a, b = _pair(workers)
    with a.active():
        database.create_profile(profile_id="p1", name="Agent", description=None, config={})
        engine.scan_all()
    converge([a, b])
    with a.active():
        database.create_tag("pruned", "pruned")
        engine.scan_all()
        profiles.delete_profile("p1")
        engine.scan_all()
        victim = next(op for op in reversed(store.ops_since({})) if op["collection"] == "tags")
        assert victim["record_id"] == "pruned"
        a.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        engine.hold_outbox(True)  # B restarted
        rule = subagents.add_permission_rule("p1", "Bash", "ls *", "allow")["id"]  # not published
    refuse = peer_snapshot._refuse_partial

    def backfilling():
        raise ValueError("This worker is still receiving its transcripts")  # HTTP 409

    monkeypatch.setattr(peer_snapshot, "_refuse_partial", backfilling)
    served = ServedBy(a, "http://a.local:8000")
    try:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            async with httpx.AsyncClient(transport=served.transport()) as client:
                with pytest.raises(transport.PeerError, match="could not fill missing updates"):
                    await link.sync_once(client, engine.require_context(), catch_up=True)
                assert engine.outbox_held()  # the deferred copy kept it closed
                assert engine.scan_all() == 0  # the scanner wakes: nothing is signed
                assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = 'permission_rules'")[0]["n"] == 0
                monkeypatch.setattr(peer_snapshot, "_refuse_partial", refuse)  # A finished its backfill
                summary = await link.sync_once(client, engine.require_context())
                assert summary.get("snapshot_required") and not summary.get("snapshot_deferred"), summary
            assert not engine.outbox_held(), summary  # the applied copy opened the outbox
            assert b.sql("SELECT COUNT(*) AS n FROM permission_rules")[0]["n"] == 0
            database.create_tag("after-b", "after-b")  # a later edit on B's stream
        _settle(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    for worker in (a, b):
        assert worker.sql("SELECT COUNT(*) AS n FROM permission_rules")[0]["n"] == 0, worker.name
        assert worker.sql("SELECT COUNT(*) AS n FROM tags WHERE id = 'after-b'")[0]["n"] == 1, worker.name
    assert rule in [c["record_id"] for c in _conflicts_in(b, "permission_rules")
                    if c["losing_device_id"] == b.device_id]
    assert b.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = 'permission_rules'")[0]["n"] == 0
    _heads_reached(a, b)


async def test_a_held_snooze_survives_a_required_state_copy_through_the_transport(workers, monkeypatch):
    """S1 through the S7 window: the copy's guarded apply signs B's held
    snooze (``_sign_pending_membership``) before A's newer version lands."""
    import httpx

    from app.core import session_snooze
    from app.network import session_active, transport
    from tests.core.network.test_control import _new_session
    from tests.core.network.test_transport import ServedBy
    monkeypatch.setattr(engine, "kick", lambda: None)
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "chat", "Chat")
    with a.active():
        session_active.write_shared("chat", False)
    converge([a, b])
    wake = session_snooze.canonical(datetime.now(timezone.utc) + timedelta(days=1))
    with a.active():
        database.create_tag("pruned", "pruned")
        engine.scan_all()
        session_active.write_shared("chat", True)  # newer than B's last signed version
        victim = next(op for op in reversed(store.ops_since({})) if op["collection"] == "tags")
        a.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        engine.hold_outbox(True)  # B restarted
        session_active.write_shared("chat", False, wake)  # the snooze is not signed
        assert _membership(b, "chat") == (False, wake, True)
    served = ServedBy(a, "http://a.local:8000")
    outcomes = []
    try:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            async with httpx.AsyncClient(transport=served.transport()) as client:
                outcomes.append(await link.sync_once(client, engine.require_context()))
            assert not engine.outbox_held(), outcomes
            assert outcomes[0].get("snapshot_required"), outcomes
        _settle(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    for worker in (a, b):
        assert _membership(worker, "chat") == (False, wake, False), worker.name
        losers = [(c["losing_device_id"], c["losing_record"].get("is_active")) for c in
                  _conflicts(worker, "session_active", "chat")]
        assert (a.device_id, True) in losers, (worker.name, losers)
    _heads_reached(a, b)


# -- S7, the hold's own guarantee: the rollback check still runs before any
# -- release, and a claim past our log that no signed op proves keeps the
# -- hold. The copy then signs nothing; it waits on the unsigned edit.

async def test_an_unproven_claim_past_our_log_keeps_the_hold_through_a_required_copy(workers):
    import httpx

    from app.db import templates
    from app.network import transport
    from tests.core.network.test_transport import ServedBy
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        templates.create_template("t", "original", "prompt")
        engine.scan_all()
    join(a, b)
    with a.active():
        database.create_tag("lost", "lost")
        engine.scan_all()
    converge([a, b])
    with a.active():
        [lost] = [op for op in store.ops_since({}) if op["record_id"] == "lost"]
    # A was restored from a backup taken before "lost"; B pruned that op, so
    # B's claim past A's log comes with no proof.
    for worker in (a, b):
        worker.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
                   (lost["origin_device_id"], lost["origin_incarnation"], lost["origin_seq"]))
    with b.active():
        templates.update_template("t", name="newer on b")
        database.create_tag("pruned", "pruned")
        engine.scan_all()
        victim = next(op for op in reversed(store.ops_since({})) if op["collection"] == "tags")
        b.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    with a.active():
        store.touch_member(b.device_id, endpoints=["http://b.local:8000"])
        engine.hold_outbox(True)  # A restarted
        templates.update_template("t", name="edited on a")  # not published
        signed = a.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE origin_device_id = ?", (a.device_id,))[0]["n"]
    served = ServedBy(b, "http://b.local:8000")
    outcomes = []
    try:
        with a.active():
            link = transport.PeerLink(b.device_id, [served.base_url])
            async with httpx.AsyncClient(transport=served.transport()) as client:
                for _ in range(2):
                    try:
                        outcomes.append(await link.sync_once(client, engine.require_context()))
                    except Exception as exc:  # noqa: BLE001 - recorded for the assertions below
                        outcomes.append(repr(exc))
            assert engine.outbox_held(), outcomes
            assert all(engine.LOCAL_CHANGE_WAITS in str(outcome) for outcome in outcomes), outcomes
            assert engine.context().identity.incarnation == 1
            assert templates.get_template("t")["name"] == "edited on a"
        assert ("GET", f"/api/v1/network/peer/ops/{a.device_id}/1/{lost['origin_seq']}") in served.requests
        assert a.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE origin_device_id = ?",
                     (a.device_id,))[0]["n"] == signed  # the copy signed nothing
    finally:
        with a.active():
            engine.hold_outbox(False)


def test_an_inbound_view_waits_for_our_state_copy_from_the_same_peer(workers):
    """While our exchange with a peer applies a required state copy, that
    peer's inbound view does not open the hold: our exchange decides once
    the copy applied. Outside a copy the same peer's view still opens it
    while our exchange with it runs (a dial that cannot reach it, #1621)."""
    from app.network import transport
    from tests.core.network.test_unscanned_local_edit import _pair
    a, b = _pair(workers)
    link = transport.PeerLink(a.device_id, ["http://a.local:8000"])
    transport._links[a.device_id] = link
    try:
        with b.active():
            engine.hold_outbox(True)

            async def copying():
                async with link._sync_lock:
                    link.copying_held = True
                    assert transport.release_hold_for_peer_view(a.device_id, {}) is False
                    assert engine.outbox_held()
                    assert transport.release_hold_for_peer_view(a.device_id, {}, own_exchange=True) is True
                    link.copying_held = False
                    engine.hold_outbox(True)
                    assert transport.release_hold_for_peer_view(a.device_id, {}) is True
            asyncio.run(copying())
            assert not engine.outbox_held()
    finally:
        transport._links.pop(a.device_id, None)
        with b.active():
            engine.hold_outbox(False)
