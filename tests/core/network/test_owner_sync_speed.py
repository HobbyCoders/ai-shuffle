"""Owner-side publish wakes, fair event publishing, chunked apply and fair pulls."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.db import database, interactions
from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, engine, startup, transport
from tests.core.network.harness import converge, join, owner_pref, set_owner_pref
from tests.core.network.test_scan_pacing import ScanClock


@pytest.fixture
def session_hook(monkeypatch):
    """Install the production post-commit hook for one test."""
    monkeypatch.setattr(events_db, "_session_write_hook", None)
    startup._install_session_write_hook()
    yield
    transport.reset_for_tests()


def _session(sid, *, owner=None):
    database.create_session(session_id=sid, profile_id="default", project_id=None, title=sid)
    if owner is not None:
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id = %s WHERE id = %s", (owner, sid))


def _append(sid, count=1):
    events_db.append_events(sid, [{"type": "message.user", "payload": {"type": "message.user", "content": f"m{i}"}}
                                  for i in range(count)])


# ---------------------------------------------------------------------------
# A. Owner writes wake the scanner
# ---------------------------------------------------------------------------

async def test_owned_event_append_wakes_a_narrow_catalog_pass(workers, session_hook, monkeypatch):
    worker = workers("owner-wake")
    clock = ScanClock(monkeypatch, passes=2)
    scan = Mock(return_value=0)
    with worker.active():
        engine.create_network("Owner wake")
        _session("mine")
        monkeypatch.setattr(engine, "scan_all", scan)
        clock.actions = [(1, lambda: _append("mine"))]
        await clock.run()
    assert clock.scans == [0, 2]
    assert scan.call_args_list[1].kwargs == {
        "collections": {"session_events"}, "include_catalog": True, "strict": True, "session_ids": ["mine"]}


async def test_mirrored_session_write_does_not_widen_or_run_a_catalog_pass(workers, session_hook, monkeypatch):
    worker = workers("mirror-wake")
    clock = ScanClock(monkeypatch, passes=2)
    scan = Mock(return_value=0)
    with worker.active():
        engine.create_network("Mirror wake")
        _session("theirs", owner="another-device")
        monkeypatch.setattr(engine, "scan_all", scan)
        clock.actions = [(1, lambda: _append("theirs"))]
        await clock.run()
    # The wake still ran a pass (its owner check), but published nothing:
    # no whole-catalog reconcile for a session another device owns.
    assert clock.scans == [0, 2]
    assert [call.kwargs for call in scan.call_args_list] == [{}]


async def test_stored_screenshot_publishes_with_the_event_that_names_it(workers, session_hook, monkeypatch, tmp_path):
    """A viewer worker gets the artifact row in the same pass as the tool.result.

    The live chat on another worker renders the image as soon as the event
    arrives; its artifact request 404s until the row is there. Without a wake
    the row waited for the next full scan (up to SCAN_IDLE_MAX_SECONDS).
    """
    from app.core import artifact_store
    from app.core.config import settings
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    worker = workers("artifact-wake")
    clock = ScanClock(monkeypatch, passes=2)
    real_scan_all = engine.scan_all
    published = []

    def scan(**kwargs):
        count = real_scan_all(**kwargs)
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT collection, record_id FROM sync_ops ORDER BY seq")
            published.append([(r["collection"], r["record_id"]) for r in cursor.fetchall()])
        return count

    with worker.active():
        engine.create_network("Artifact wake")
        _session("chat")
        monkeypatch.setattr(engine, "scan_all", scan)

        def read_image():
            row = artifact_store.store_bytes(b"jpeg bytes", mime="image/jpeg", created_by_session="chat",
                                             runner_id=settings.effective_instance_id)
            events_db.append_events("chat", [{"type": "tool.result", "payload": {
                "tool_use_id": "t1", "artifact_hashes": [row["hash"]]}}])
            return row

        holder = {}
        clock.actions = [(1, lambda: holder.update(row=read_image()))]
        await clock.run()
    digest = holder["row"]["hash"]
    assert clock.scans == [0, 2]  # the wake pass, not the next full scan
    ops = published[1]
    assert ("artifacts", digest) in ops
    event_ops = [i for i, (coll, rid) in enumerate(ops) if coll == "session_events" and rid.startswith("chat|")]
    assert event_ops and ops.index(("artifacts", digest)) < event_ops[-1]


def test_prompt_and_run_transitions_name_their_session(workers, session_hook):
    from app.core import session_runtime
    worker = workers("prompt-wake")
    with worker.active():
        engine.create_network("Prompt wake")
        _session("s1")
        transport.reset_for_tests()
        row = interactions.create_interaction("s1", "permission", {"tool": "Bash"}, None)
        assert transport._owner_candidates == {"s1"}
        assert transport._owner_collections == {"pending_interactions"}
        transport.reset_for_tests()
        assert interactions.answer_interaction(row["id"], {"decision": "allow"})
        assert transport._owner_candidates == {"s1"}
        transport.reset_for_tests()
        second = interactions.create_interaction("s1", "permission", {}, None)
        transport.reset_for_tests()
        assert interactions.cancel_interaction(second["id"])
        assert transport._owner_candidates == {"s1"}
        transport.reset_for_tests()
        assert not interactions.cancel_interaction(second["id"])  # nothing changed, no wake
        assert transport._owner_candidates == set()
        session_runtime._notify_run_change("s1")
        assert transport._owner_candidates == {"s1"}
        assert transport._owner_collections == {"session_runtime"}


def test_hook_is_a_no_op_before_enrollment(workers, session_hook):
    worker = workers("standalone")
    with worker.active():
        assert engine.cached_context() is None
        _session("s1")
        _append("s1")
        interactions.create_interaction("s1", "permission", {}, None)
    assert transport._owner_candidates == set()


def test_hook_failure_never_breaks_the_write(workers, monkeypatch):
    worker = workers("broken-hook")
    monkeypatch.setattr(events_db, "_session_write_hook", Mock(side_effect=RuntimeError("boom")))
    with worker.active():
        _session("s1")
        assert _append("s1") is None
        assert events_db.get_max_seq("s1") == 1


# ---------------------------------------------------------------------------
# B. Fair, recent-first event publishing
# ---------------------------------------------------------------------------

def _cursors(*sids):
    with store.get_db() as conn:
        cursor = conn.cursor()
        return {sid: store.catalog_cursor(cursor, sid) for sid in sids}


def test_recent_sessions_publish_first_and_share_the_budget(workers):
    worker = workers("fair-events")
    with worker.active():
        engine.create_network("Fair events")
        now = datetime.now(timezone.utc)
        for sid, events, hours_ago in (("old", 10, 30), ("mid", 3, 20), ("live", 2, 1)):
            _session(sid)
            _append(sid, events)
            stamp = (now - timedelta(hours=hours_ago)).isoformat()
            worker.sql("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, sid))
        # One event of budget goes to the most recently active session.
        assert catalog.publish_events(limit=1) == 1
        assert _cursors("old", "mid", "live") == {"old": 0, "mid": 0, "live": 1}
        # Six events: the most recent sessions take what they have first
        # (#1978), so the old backlog cannot take them all; it gets the rest.
        assert catalog.publish_events(limit=6) == 6
        assert _cursors("old", "mid", "live") == {"old": 2, "mid": 3, "live": 2}


def test_backlog_publishes_the_recent_top_level_chat_before_subagent_transcripts(workers, monkeypatch):
    """After a long hold the open chat is current first, sub-agents follow (#1978)."""
    worker = workers("priority-events")
    monkeypatch.setattr(catalog, "EVENT_BATCH", 10)
    monkeypatch.setattr(catalog, "EVENT_CHUNK", 4)
    with worker.active():
        engine.create_network("Priority events")
        now = datetime.now(timezone.utc)
        top_level = {"open-chat": (40, 1), "older-chat": (6, 50)}
        # Sub-agents that ran for the open chat are its most recent activity.
        subagents = {f"agent-{i}": (12, 0.1 * (i + 1)) for i in range(6)}
        subagents["old-agent"] = (12, 60)
        for sid, (events, hours_ago) in {**top_level, **subagents}.items():
            _session(sid)
            _append(sid, events)
            worker.sql("UPDATE sessions SET updated_at = ?, is_worker = ? WHERE id = ?",
                       ((now - timedelta(hours=hours_ago)).isoformat(), sid in subagents, sid))
        sizes = {sid: events for sid, (events, _) in {**top_level, **subagents}.items()}
        passes = 0
        while catalog.publish_owned():
            passes += 1
            assert passes < 100
        assert _cursors(*sizes) == sizes
        # The outbox order is the publish order (origin_seq), and the order
        # in which a returning peer pulls it.
        ops = sorted((o for o in store.ops_since({}) if o["collection"] == "session_events"),
                     key=lambda o: o["origin_seq"])
        owner = [o["record_id"].split("|", 1)[0] for o in ops]
        last = {sid: max(i for i, s in enumerate(owner) if s == sid) for sid in sizes}
        first_subagent = min(i for i, s in enumerate(owner) if s in subagents)
        assert last["open-chat"] < last["older-chat"] < first_subagent
        assert max(last.values()) == last["old-agent"]
        # Per-session order is kept: every stream went out in seq order.
        by_session = {}
        for op, sid in zip(ops, owner):
            by_session.setdefault(sid, []).append(op["order_seq"])
        assert all(seqs == list(range(1, sizes[sid] + 1)) for sid, seqs in by_session.items())


def test_one_scanner_pass_drains_a_backlog_in_short_transactions(workers, monkeypatch):
    worker = workers("backlog")
    monkeypatch.setattr(catalog, "EVENT_BATCH", 5)
    monkeypatch.setattr(catalog, "EVENT_CHUNK", 2)
    with worker.active():
        engine.create_network("Backlog")
        _session("s1")
        _append("s1", 12)
        commits = Mock(wraps=catalog.get_db)
        monkeypatch.setattr(catalog, "get_db", commits)
        catalog.publish_owned()
        assert _cursors("s1") == {"s1": 12}
        # Several short transactions rather than one holding the state lock.
        assert commits.call_count >= 12 // 2
        ops = [o for o in store.ops_since({}) if o["collection"] == "session_events"]
        assert sorted(o["order_seq"] for o in ops) == list(range(1, 13))


def test_a_pass_that_leaves_a_backlog_tells_the_scanner(workers, monkeypatch):
    """#1978: the scanner rests less while events remain unpublished; the
    publisher reports that once per pass, and only while it is true."""
    worker = workers("backlog-flag")
    monkeypatch.setattr(catalog, "EVENT_BATCH", 5)
    monkeypatch.setattr(catalog, "EVENT_PASS_BATCHES", 2)
    with worker.active():
        engine.create_network("Backlog")
        _session("s1")
        _append("s1", 23)
        catalog.publish_owned()
        assert _cursors("s1") == {"s1": 10}
        assert catalog.take_events_backlog() is True
        assert catalog.take_events_backlog() is False  # read once
        catalog.publish_owned()
        assert catalog.take_events_backlog() is True
        catalog.publish_owned()  # 3 left: a partial batch ends the backlog
        assert _cursors("s1") == {"s1": 23}
        assert catalog.take_events_backlog() is False


def test_hydrate_publishes_a_long_transcript_in_chunks(workers, monkeypatch):
    worker = workers("hydrate")
    monkeypatch.setattr(catalog, "EVENT_CHUNK", 3)
    with worker.active():
        engine.create_network("Hydrate")
        _session("s1")
        _append("s1", 10)
        assert catalog.hydrate("s1") == 10
        assert _cursors("s1") == {"s1": 10}


# ---------------------------------------------------------------------------
# C. Chunked apply
# ---------------------------------------------------------------------------

def _pref_ops(sender, receiver, keys):
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops = store.ops_since(marks, exclude_origin=receiver.device_id)
    wanted = [o for o in ops if o["record_id"].rsplit("|", 1)[-1] in keys]
    return sorted(wanted, key=lambda o: o["origin_seq"])


def test_chunk_applies_in_one_transaction_and_observes_the_clock_once(workers, monkeypatch):
    a, b = workers("chunk-a"), workers("chunk-b")
    with a.active():
        engine.create_network("Chunks")
    join(a, b)
    converge([a, b])
    keys = [f"k{i}" for i in range(5)]
    for i, key in enumerate(keys):
        set_owner_pref(a, key, i)
    ops = _pref_ops(a, b, keys)
    assert len(ops) == 5
    observed = Mock(wraps=store.observe_clock)
    monkeypatch.setattr(store, "observe_clock", observed)
    with b.active():
        result = engine.apply_batch(ops)
    assert result.applied == 5 and result.rejected == result.retry == 0
    assert observed.call_count == 1
    assert observed.call_args.args[1] == max(o["clock"] for o in ops)
    assert [owner_pref(b, key) for key in keys] == list(range(5))


def test_bad_envelope_inside_a_chunk_keeps_watermark_semantics(workers):
    a, b = workers("bad-a"), workers("bad-b")
    with a.active():
        engine.create_network("Bad chunk")
    join(a, b)
    converge([a, b])
    keys = [f"k{i}" for i in range(5)]
    for i, key in enumerate(keys):
        set_owner_pref(a, key, i)
    ops = _pref_ops(a, b, keys)
    forged = dict(ops[2], clock=ops[2]["clock"] + 5)
    with b.active():
        result = engine.apply_batch([ops[0], ops[1], forged, ops[3], ops[4]])
        # Without savepoints (the SQLite shim) the chunk is replayed one
        # envelope per transaction; the harness shares one connection, so a
        # nested read-only get_db() may already have committed an earlier op,
        # which the replay then counts as a duplicate.
        assert result.applied + result.duplicate == 2
        assert result.rejected == 1 and result.pending == 2
        assert "bad signature" in result.reasons
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), a.device_id, 1) == ops[1]["origin_seq"]
        # The genuine op fills the hole and the buffered ones follow it.
        assert engine.apply_batch([ops[2]]).applied == 3
    assert [owner_pref(b, key) for key in keys] == list(range(5))


def test_membership_replay_uses_generation_even_when_receive_timestamps_tie(workers, monkeypatch):
    worker = workers("membership-order")
    monkeypatch.setattr(store, "_now", lambda: "2026-10-08T00:00:00+00:00")
    with worker.active():
        store.put_membership_record("cert:z-founder", {"generation": 0})
        store.put_membership_record("cert:a-child", {"generation": 1})
        store.put_membership_record("cert:b-grandchild", {"generation": 2})
        assert [row["record"]["generation"] for row in store.list_membership_records()] == [0, 1, 2]


def test_fresh_transcript_delta_uses_bulk_sql_and_one_mirror_wake_per_chunk(workers, monkeypatch):
    from tests.core.network.test_ownership_revocation import _network
    a, b = _network(workers, "bulk-a", "bulk-b")
    with a.active():
        _session("bulk")
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        _append("bulk", 400)
        catalog.publish_owned()
        ops = store.ops_since(marks, exclude_origin=b.device_id)
    assert len(ops) == 400
    real_insert = store.insert_event_batch
    bulk = Mock(wraps=real_insert)
    mirrored = Mock(wraps=catalog.after_event_mirrored)
    monkeypatch.setattr(store, "insert_event_batch", bulk)
    monkeypatch.setattr(catalog, "after_event_mirrored", mirrored)
    statements = []
    b.raw.set_trace_callback(statements.append)
    try:
        with b.active():
            result = engine.apply_batch(ops)
            assert not engine.applying_batch()
    finally:
        b.raw.set_trace_callback(None)
    assert result.applied == 400 and result.retry == result.rejected == result.pending == 0
    assert bulk.call_count == mirrored.call_count == 7  # ceil(400 / APPLY_CHUNK=64)
    assert len(statements) <= 20 * 7  # not ~19 statements * 400 envelopes
    assert [row["seq"] for row in b.sql("SELECT seq FROM session_events WHERE session_id = ? ORDER BY seq", ("bulk",))] == list(range(1, 401))
    assert b.sql("SELECT next_seq FROM session_event_counters WHERE session_id = ?", ("bulk",))[0]["next_seq"] == 400
    assert b.sql("SELECT published_seq FROM network_catalog_cursors WHERE session_id = ?", ("bulk",))[0]["published_seq"] == 400
    with b.active():
        assert engine.apply_batch(ops).duplicate == 400


@pytest.mark.parametrize("fault", ["signature", "hole", "rewrite", "commit"])
def test_event_bulk_fallback_preserves_order_and_retries(workers, monkeypatch, fault):
    from tests.core.network.test_ownership_revocation import _network
    a, b = _network(workers, "fallback-a", "fallback-b")
    with a.active():
        _session("fallback")
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        _append("fallback", 5)
        catalog.publish_owned()
        ops = store.ops_since(marks, exclude_origin=b.device_id)
    real_insert = store.insert_event_batch
    bulk = Mock(wraps=real_insert)
    monkeypatch.setattr(store, "insert_event_batch", bulk)
    with b.active():
        if fault == "signature":
            forged = dict(ops[2], clock=ops[2]["clock"] + 1)
            result = engine.apply_batch([*ops[:2], forged, *ops[3:]])
            assert result.rejected == 1 and result.pending == 2
            assert engine.apply_batch([ops[2]]).applied == 3
        elif fault == "hole":
            assert engine.apply_batch(ops[1:]).pending == 4
            assert engine.apply_batch(ops[:1]).applied == 5
        elif fault == "rewrite":
            assert engine.apply_batch(ops).applied == 5
            with a.active():
                a.sql("UPDATE session_events SET payload = ? WHERE session_id = ? AND seq = 3",
                      ('{"content":"rewritten"}', "fallback"))
                a.sql("DELETE FROM network_catalog_cursors WHERE session_id = ?", ("fallback",))
                catalog.publish_owned()
            marks = store.local_watermarks()
            with a.active():
                changed = store.ops_since(marks, exclude_origin=b.device_id)
            assert engine.apply_batch(changed).retry == 0
            assert 'rewritten' in b.sql("SELECT payload FROM session_events WHERE session_id = ? AND seq = 3", ("fallback",))[0]["payload"]
        else:
            def fail_after_writes(*args):
                real_insert(*args)
                raise RuntimeError("bulk storage failed before commit")
            bulk.side_effect = fail_after_writes
            result = engine.apply_batch(ops)
            assert result.applied == 5 and result.retry == result.rejected == 0
        assert not engine.applying_batch()
    assert [row["seq"] for row in b.sql("SELECT seq FROM session_events WHERE session_id = ? ORDER BY seq", ("fallback",))] == list(range(1, 6))


# ---------------------------------------------------------------------------
# D. Fair budget between lagging streams
# ---------------------------------------------------------------------------

def test_ops_since_gives_every_lagging_stream_a_share(workers):
    a, b, c = workers("fair-a"), workers("fair-b"), workers("fair-c")
    with a.active():
        engine.create_network("Fair pulls")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    with c.active():
        marks = store.local_watermarks()
    for i in range(6):
        set_owner_pref(a, f"a{i}", i)
    set_owner_pref(b, "b0", 0)
    converge([a, b])
    with a.active():
        tiny = store.ops_since(marks, limit=2, exclude_origin=c.device_id)
        page = store.ops_since(marks, limit=4, exclude_origin=c.device_id)
        everything = store.ops_since(marks, exclude_origin=c.device_id)
    # Two lagging origins, two ops: one each, not both from the first stream.
    assert sorted(o["origin_device_id"] for o in tiny) == sorted([a.device_id, b.device_id])
    lag_b = sum(o["origin_device_id"] == b.device_id for o in everything)
    by_origin = {}
    for op in page:
        by_origin.setdefault(op["origin_device_id"], []).append(op["origin_seq"])
    assert len(page) == 4 and set(by_origin) == {a.device_id, b.device_id}
    assert len(by_origin[b.device_id]) == min(2, lag_b)
    # Each stream's slice is a contiguous prefix past the caller's watermark.
    for origin, seqs in by_origin.items():
        start = int((marks.get(origin) or {}).get("1", 0)) + 1
        assert seqs == list(range(start, start + len(seqs)))


# ---------------------------------------------------------------------------
# F. Liveness, G. mirrored transcript invalidations
# ---------------------------------------------------------------------------

def test_inbound_contact_ends_our_backoff_towards_that_peer(monkeypatch):
    import time
    kicks = Mock()
    monkeypatch.setattr(engine, "kick", kicks)
    link = transport.PeerLink("peer", ["https://peer.example.com"])
    link.failures, link.backoff_until = 3, time.monotonic() + 60
    monkeypatch.setitem(transport._links, "peer", link)
    transport.note_contact("peer")
    # The backoff ends but the streak is remembered: a redial that fails
    # again backs off from where it was, not from the first step.
    assert link.backoff_until == 0.0 and link.failures == 3
    assert kicks.call_count == 1
    # With one-way reachability the redial keeps failing while the peer
    # keeps calling us; only the first contact of a streak cuts it short.
    link.failures, link.backoff_until = 4, time.monotonic() + 60
    transport.note_contact("peer")
    assert kicks.call_count == 1 and link.backoff_until > 0.0
    # A successful sync ends the streak; the next streak gets one again.
    link.failures, link.backoff_until, link.contact_kick_used = 0, 0.0, False
    link.failures, link.backoff_until = 1, time.monotonic() + 4
    transport.note_contact("peer")
    assert kicks.call_count == 2 and link.backoff_until == 0.0
    # A healthy link is not kicked on every inbound exchange.
    link.failures, link.contact_kick_used = 0, False
    transport.note_contact("peer")
    assert kicks.call_count == 2
    transport.forget_peers()


def test_mirrored_events_alone_coalesce_session_list_invalidations(monkeypatch):
    from types import SimpleNamespace
    from app.core.global_events import global_events

    changed = Mock()
    scheduled = []
    monkeypatch.setattr(global_events, "resources_changed", changed)
    monkeypatch.setattr(global_events, "_loop", SimpleNamespace(
        call_soon_threadsafe=lambda fn, *args: scheduled.append(args), call_later=None), raising=False)
    monkeypatch.setattr(startup, "_events_only_last", float("-inf"))
    monkeypatch.setattr(startup, "_events_only_trailing", False)

    def sessions_emits():
        return sum(call.args[0] == "sessions" for call in changed.call_args_list)

    events = [("session_events", "s1|1", "upsert")]
    startup._on_applied(events)
    assert sessions_emits() == 4  # admin, api_user, account, paired
    startup._on_applied(events)
    startup._on_applied(events)
    assert sessions_emits() == 4 and len(scheduled) == 1  # one trailing refresh queued
    # Anything else about a session still invalidates at once.
    startup._on_applied([("session_runtime", "s1", "upsert")])
    assert sessions_emits() == 8
    delay, callback = scheduled[0]
    assert 0 < delay <= startup.EVENTS_ONLY_INVALIDATION_SECONDS
    callback()
    assert sessions_emits() == 12
