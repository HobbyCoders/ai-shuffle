from app.db import database, network as store
from app.network import engine
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session


def _delete_tag_after_clock(worker, tag_id, child_clock):
    """Normal producer edits establish a strictly newer Lamport deletion."""
    with worker.active():
        revision = 0
        while worker.sql("SELECT clock FROM sync_records WHERE collection='tags' AND record_id=?",
                         (tag_id,))[0]["clock"] <= child_clock:
            revision += 1
            database.update_tag(tag_id, name=f"Revision {revision}")
            engine.scan_all()
        database.delete_tag(tag_id)
        engine.scan_all()


def test_refused_child_preserves_relay_log(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Gap")
    join(a, b)
    join(a, c)
    _new_session(a, "s", "S")
    converge([a, b, c])
    with b.active():
        database.create_tag("t", "T")
        engine.scan_all()
    exchange(b, a)
    exchange(b, c)
    with a.active():
        database.add_session_tag("s", "t")
        engine.scan_all()
        child_clock = a.sql("SELECT clock FROM sync_records WHERE collection='session_tags'")[0]["clock"]
    _delete_tag_after_clock(b, "t", child_clock)
    exchange(b, a)
    exchange(b, c)
    with a.active():
        database.set_session_active("s", False)
        engine.scan_all()
    res = exchange(a, b)
    assert res.rejected == 1 and res.retry == 0
    # C hears A's stream only through B.
    with c.active():
        marks = store.local_watermarks()
    with b.active():
        ops, _, needs = store.exchange_batch(marks, limit=500, exclude_origin=c.device_id)
    assert needs is False, "a refused signed operation must remain forwardable"
    with c.active():
        result = engine.apply_batch(ops)
        assert result.retry == 0
        assert not database.get_session("s")["is_active"]
        assert not store.pending_holes()


def _deliver_from_origin(sender, receiver, origin):
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops, _, _ = store.exchange_batch(marks, limit=500, exclude_origin=receiver.device_id)
    ops = [op for op in ops if op["origin_device_id"] == origin]
    with receiver.active():
        return engine.apply_batch(ops)


def test_recreated_agent_retains_deny_rule(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Probe")
    join(a, b)
    join(a, c)
    with a.active():
        database.create_profile("rev", "Rev", None, {})
        engine.scan_all()
    converge([a, b, c])
    with a.active():
        database.delete_profile("rev")
        engine.scan_all()
    converge([a, b, c])
    with c.active():
        database.create_profile("rev", "Rev", None, {})  # same id, derived from name
        engine.scan_all()
    exchange(c, a)
    with a.active():
        assert database.get_profile("rev")
        rule = database.add_permission_rule("rev", "Bash", None, "deny")
        engine.scan_all()
    # B hears A's stream before C's re-creation.
    res = _deliver_from_origin(a, b, a.device_id)
    assert res.retry == 1 and res.rejected == 0
    exchange(c, b)
    converge([a, b, c])
    with b.active():
        assert database.get_profile("rev"), "parent re-created"
        rows = b.sql("SELECT id, decision FROM permission_rules WHERE profile_id='rev'")
        assert rows == [{"id": rule["id"], "decision": "deny"}]


def test_child_newer_than_tag_tombstone_waits_for_recreated_parent(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Recreation")
    join(a, b)
    join(a, c)
    _new_session(a, "s", "S")
    with a.active():
        database.create_tag("reused", "Before")
        engine.scan_all()
    converge([a, b, c])
    with a.active():
        database.delete_tag("reused")
        engine.scan_all()
    converge([a, b, c])
    with c.active():
        database.create_tag("reused", "After")
        engine.scan_all()
    exchange(c, a)
    with a.active():
        database.add_session_tag("s", "reused")
        engine.scan_all()
    result = _deliver_from_origin(a, b, a.device_id)
    assert result.retry == 1 and result.rejected == 0
    exchange(c, b)
    converge([a, b, c])
    with b.active():
        assert database.get_session_tags("s")[0]["id"] == "reused"
        assert not store.pending_holes()
