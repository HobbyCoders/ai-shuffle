"""Idle scan pacing, wake coalescing, and the post-commit wake producer."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.network import engine, transport


class ScanClock:
    """Drive the real loop through virtual waits, without wall-clock sleeps."""

    def __init__(self, monkeypatch, passes):
        self.now = 0.0
        self.passes = passes
        self.scans = []
        self.actions = []
        monkeypatch.setattr(transport, "_scan_wake", None)
        monkeypatch.setattr(transport, "_dirty_collections", set())
        monkeypatch.setattr(transport, "_dirty_sessions", set())
        monkeypatch.setattr(transport, "_full_scan_requested", False)
        monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(transport, "_loop", SimpleNamespace(
            call_soon_threadsafe=lambda callback: callback()))
        monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
            Event=asyncio.Event, CancelledError=asyncio.CancelledError,
            TimeoutError=asyncio.TimeoutError, sleep=self.sleep,
            wait_for=self.wait_for, to_thread=self.to_thread))

    async def to_thread(self, fn, **kwargs):
        if len(self.scans) == self.passes:
            raise asyncio.CancelledError
        self.scans.append(self.now)
        return fn(**kwargs)

    def advance(self, until, *, interrupt=False):
        while self.actions and self.actions[0][0] <= until:
            self.now, action = self.actions.pop(0)
            action()
            if interrupt and transport._scan_wake.is_set():
                return
        self.now = until

    async def sleep(self, seconds):
        self.advance(self.now + seconds)

    async def wait_for(self, coro, *, timeout):
        coro.close()
        self.advance(self.now + timeout, interrupt=True)
        if not transport._scan_wake.is_set():
            raise asyncio.TimeoutError

    async def run(self):
        with pytest.raises(asyncio.CancelledError):
            await transport.scan_loop()


async def test_quiet_scans_back_off_and_keep_periodic_reconciliation(monkeypatch):
    clock = ScanClock(monkeypatch, passes=8)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    await clock.run()
    assert clock.scans == [0, 4, 12, 28, 60, 120, 180, 240]
    assert scan.call_count == 8


async def test_published_changes_restore_active_cadence(monkeypatch):
    clock = ScanClock(monkeypatch, passes=8)
    monkeypatch.setattr(engine, "scan_all", Mock(side_effect=[0, 0, 0, 0, 1, 1, 0, 0]))
    await clock.run()
    assert clock.scans == [0, 4, 12, 28, 60, 62, 64, 68]


async def test_writes_during_scan_are_retained_and_bursts_are_coalesced(monkeypatch):
    clock = ScanClock(monkeypatch, passes=3)

    def scan():
        # Simulate a write after its collection has already been scanned.
        transport.request_scan()
        transport.request_scan()
        return 0

    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(0.5, transport.request_scan), (1, transport.request_scan)]
    await clock.run()
    assert clock.scans == [0, 2, 4]


async def test_failures_back_off_instead_of_retrying_in_a_tight_loop(monkeypatch):
    clock = ScanClock(monkeypatch, passes=7)
    monkeypatch.setattr(engine, "scan_all", Mock(side_effect=RuntimeError("scan failed")))
    await clock.run()
    assert clock.scans == [0, 4, 12, 28, 60, 120, 180]


@pytest.mark.parametrize("phase", ["cooldown", "idle_wait"])
async def test_shutdown_cancels_either_wait(monkeypatch, phase):
    clock = ScanClock(monkeypatch, passes=2)
    monkeypatch.setattr(engine, "scan_all", Mock(return_value=0))

    async def cancel(*args, **kwargs):
        if phase == "idle_wait":
            args[0].close()
        raise asyncio.CancelledError

    monkeypatch.setattr(transport.asyncio, "sleep" if phase == "cooldown" else "wait_for", cancel)
    await clock.run()
    assert clock.scans == [0]


async def test_committed_profile_write_wakes_idle_scanner(monkeypatch, tmp_path):
    from app.core import resource_sync
    from app.db import database
    from app.db import network as store
    from app.network import startup, tokens
    from tests.core.network.harness import Worker

    worker = Worker("idle-scan", tmp_path)
    clock = ScanClock(monkeypatch, passes=6)
    # Install the production post-commit hook, restoring its globals on exit.
    monkeypatch.setattr(resource_sync, "_network_hooked", False, raising=False)
    monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
    startup._install_resource_hook()
    try:
        with worker.active():
            engine.create_network("Idle scanner test")
            # Isolate the real collection publisher from unrelated filesystem scans.
            engine.scan_collection("agents")
            monkeypatch.setattr(engine, "scan_all", lambda **kwargs: engine.scan_collection("agents"))
            clock.actions = [(65, lambda: database.create_profile(
                profile_id="wake-test", name="Wake test", description=None, config={}))]
            await clock.run()
            assert clock.scans == [0, 4, 12, 28, 60, 65]
            with store.get_db() as conn:
                row = store.get_record(conn.cursor(), "agents", "wake-test")
            assert row is not None
            assert not row["deleted"]
    finally:
        worker.close()
        engine.reset_for_tests()
        tokens.reset_for_tests()


async def test_known_write_wakes_only_its_collections(monkeypatch):
    clock = ScanClock(monkeypatch, passes=3)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(["agents"])),
                     (1.5, lambda: transport.request_scan(["agents", "system_settings"]))]
    await clock.run()
    assert clock.scans == [0, 2, 6]
    assert scan.call_args_list[0].kwargs == {}
    assert scan.call_args_list[1].kwargs == {
        "collections": {"agents", "system_settings"}, "include_catalog": False, "strict": True}
    assert scan.call_args_list[2].kwargs == {}


@pytest.mark.parametrize("collection", ["agents", "sessions"])
async def test_targeted_writes_during_pass_survive_and_cannot_starve_full_scan(monkeypatch, collection):
    clock = ScanClock(monkeypatch, passes=32)
    calls = []
    def scan(**kwargs):
        calls.append(kwargs)
        transport.request_scan([collection])
        return 0
    monkeypatch.setattr(engine, "scan_all", scan)
    await clock.run()
    assert clock.scans == list(range(0, 64, 2))
    assert calls[0] == calls[30] == {}
    assert all(calls[i].get("collections") == {collection} for i in range(1, 30))
    assert calls[31]["collections"] == {collection}


@pytest.mark.parametrize("collection", ["unknown", "membership", "memory", "plugins_desired"])
async def test_unhandled_writes_request_full_reconciliation(monkeypatch, collection):
    clock = ScanClock(monkeypatch, passes=2)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(["agents"])),
                     (1.5, lambda: transport.request_scan([collection]))]
    await clock.run()
    assert [call.kwargs for call in scan.call_args_list] == [{}, {}]


@pytest.mark.parametrize("collection", ["agents", "sessions"])
async def test_failed_targeted_scan_retries_full_reconciliation(monkeypatch, collection):
    clock = ScanClock(monkeypatch, passes=3)
    scan = Mock(side_effect=[0, RuntimeError("failed targeted read"), 0])
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan([collection]))]
    await clock.run()
    assert scan.call_args_list[1].kwargs["collections"] == {collection}
    assert scan.call_args_list[2].kwargs == {}


async def test_catalog_and_shared_writes_coalesce_without_full_scan(monkeypatch):
    from app.network import policy

    clock = ScanClock(monkeypatch, passes=3)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(["agents"])),
                     (1.5, lambda: transport.request_scan(list(policy.CATALOG_COLLECTIONS)))]
    await clock.run()
    assert clock.scans == [0, 2, 6]
    assert [call.kwargs for call in scan.call_args_list] == [
        {}, {"collections": {"agents", *policy.CATALOG_COLLECTIONS},
             "include_catalog": True, "strict": True}, {}]


async def test_session_mutations_publish_without_shared_reads_and_reconcile_external_edits(monkeypatch, tmp_path):
    from app.core import resource_sync
    from app.db import database
    from app.db import network as store
    from app.network import memory_records, plugins_sync, policy, startup, tokens
    from tests.core.network.harness import Worker

    worker = Worker("catalog-wakes", tmp_path)
    clock = ScanClock(monkeypatch, passes=32)
    original_scan = engine.scan_all
    passes = []
    session_records = []
    idle_records = []
    queries = []
    memory = Mock(wraps=memory_records.publish)
    plugins = Mock(wraps=plugins_sync.publish)
    monkeypatch.setattr(memory_records, "publish", memory)
    monkeypatch.setattr(plugins_sync, "publish", plugins)

    def record(collection, record_id):
        with store.get_db() as conn:
            return store.get_record(conn.cursor(), collection, record_id)

    def scan(**kwargs):
        queries.clear()
        worker.raw.set_trace_callback(queries.append)
        try:
            count = original_scan(**kwargs)
        finally:
            worker.raw.set_trace_callback(None)
        passes.append((kwargs, list(queries), record("agents", "external")["content_hash"]))
        session_records.append(record("sessions", "changed"))
        idle_records.append(record("sessions", "idle")["content_hash"])
        if len(passes) == 3:
            # An unhooked edit to a session no wake names: after the deletion's
            # whole-catalog pass, only the periodic full pass may find it.
            worker.sql("UPDATE sessions SET title = 'Externally edited' WHERE id = 'idle'")
        if len(passes) == 1:
            # Deliberately bypass post-commit hooks: periodic reconciliation
            # must find unrelated external edits despite continuous session writes.
            worker.sql("UPDATE agents SET name = 'Externally edited' WHERE id = 'external'")
        if len(passes) == 2:
            database.delete_session("changed")
        else:
            database.update_session("busy", title=f"During scan {len(passes)}")
            if len(passes) == 1:
                database.update_session("changed", title="Changed after scan")
        return count

    try:
        with worker.active():
            engine.create_network("Catalog wake test")
            database.create_profile(profile_id="external", name="Original", description=None, config={})
            for sid in ("busy", "changed", "idle"):
                database.create_session(session_id=sid, profile_id="default", project_id=None, title="Original")
            original_scan(strict=True)
            baseline_session = record("sessions", "changed")["content_hash"]
            memory.reset_mock()
            plugins.reset_mock()
            monkeypatch.setattr(resource_sync, "_network_hooked", False, raising=False)
            monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
            startup._install_resource_hook()
            monkeypatch.setattr(engine, "scan_all", scan)
            await clock.run()
            assert clock.scans == list(range(0, 64, 2))
            assert session_records[1]["content_hash"] != baseline_session
            assert session_records[2]["deleted"]
            assert passes[0][0] == passes[30][0] == {}
            assert all(passes[i][0]["collections"] == set(policy.CATALOG_COLLECTIONS) for i in range(1, 30))
            assert passes[1][0]["session_ids"] == ["busy", "changed"]
            assert passes[2][0]["session_ids"] == ["changed"]
            assert all(passes[i][0]["session_ids"] == ["busy"] for i in range(3, 30))
            assert idle_records[2] == idle_records[29] != idle_records[30]
            assert passes[0][2] == passes[29][2] != passes[30][2]
            assert all(not any("SELECT * FROM agents" in sql for sql in passes[i][1])
                       for i in range(1, 30))
            assert any("SELECT * FROM agents" in sql for sql in passes[30][1])
            assert memory.call_count == plugins.call_count == 2
    finally:
        worker.close()
        engine.reset_for_tests()
        tokens.reset_for_tests()
        transport.reset_for_tests()


# ---------------------------------------------------------------------------
# Per-session dirty tracking
# ---------------------------------------------------------------------------

def _catalog_names():
    from app.network import policy
    return list(policy.CATALOG_COLLECTIONS)


async def test_session_ids_coalesce_and_writes_during_a_pass_stay_queued(monkeypatch):
    clock = ScanClock(monkeypatch, passes=4)
    calls = []

    def scan(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            # This write lands after the pass took its snapshot.
            transport.request_scan(_catalog_names(), session_ids=["late"])
        return 0

    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(_catalog_names(), session_ids=["b"])),
                     (1.5, lambda: transport.request_scan(_catalog_names(), session_ids=["a", "b"]))]
    await clock.run()
    catalog = set(_catalog_names())
    assert calls == [
        {},
        {"collections": catalog, "include_catalog": True, "strict": True, "session_ids": ["a", "b"]},
        {"collections": catalog, "include_catalog": True, "strict": True, "session_ids": ["late"]},
        {},
    ]


async def test_catalog_wake_without_ids_widens_to_the_whole_catalog(monkeypatch):
    clock = ScanClock(monkeypatch, passes=3)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(_catalog_names(), session_ids=["a"])),
                     (1.2, lambda: transport.request_scan(_catalog_names())),
                     (1.5, lambda: transport.request_scan(_catalog_names(), session_ids=["b"]))]
    await clock.run()
    assert scan.call_args_list[1].kwargs == {
        "collections": set(_catalog_names()), "include_catalog": True, "strict": True}
    assert scan.call_args_list[2].kwargs == {}


@pytest.mark.parametrize("extra, narrowed", [(0, True), (1, False)])
async def test_dirty_session_cap_falls_back_to_the_whole_catalog(monkeypatch, extra, narrowed):
    clock = ScanClock(monkeypatch, passes=2)
    scan = Mock(return_value=0)
    monkeypatch.setattr(engine, "scan_all", scan)
    ids = [f"s{i:04d}" for i in range(transport.SCAN_DIRTY_SESSIONS_MAX + extra)]
    half = len(ids) // 2
    clock.actions = [(1, lambda: transport.request_scan(_catalog_names(), session_ids=ids[:half])),
                     (1.5, lambda: transport.request_scan(_catalog_names(), session_ids=ids[half:]))]
    await clock.run()
    kwargs = scan.call_args_list[1].kwargs
    assert kwargs["collections"] == set(_catalog_names())
    assert kwargs.get("session_ids") == (sorted(ids) if narrowed else None)


async def test_failed_session_pass_retries_full_reconciliation(monkeypatch):
    clock = ScanClock(monkeypatch, passes=3)
    scan = Mock(side_effect=[0, RuntimeError("failed narrow read"), 0])
    monkeypatch.setattr(engine, "scan_all", scan)
    clock.actions = [(1, lambda: transport.request_scan(_catalog_names(), session_ids=["a"]))]
    await clock.run()
    assert scan.call_args_list[1].kwargs["session_ids"] == ["a"]
    assert scan.call_args_list[2].kwargs == {}


def _seed_session(sid, *, parent=None):
    """An owned session with one event, one tag and one prompt."""
    from app.db import database
    from app.db import network as store
    from app.db import session_events as events_db
    database.create_session(session_id=sid, profile_id="default", project_id=None, title=sid,
                            parent_session_id=parent)
    events_db.append_events(sid, [{"type": "message.user",
                                   "payload": {"type": "message.user", "content": "hello " + sid}}])
    with store.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO tags(id,name) VALUES (%s,%s)", (sid, sid))
        cur.execute("INSERT INTO session_tags(session_id,tag_id) VALUES (%s,%s)", (sid, sid))
        cur.execute("INSERT INTO pending_interactions(id,session_id,kind,payload) VALUES (%s,%s,%s,%s)",
                    ("p-" + sid, sid, "question", '{"question":"test"}'))


def _record(collection, record_id):
    from app.db import network as store
    with store.get_db() as conn:
        return store.get_record(conn.cursor(), collection, record_id)


def _records(sid):
    """Session, runtime, tag and prompt records of one seeded session."""
    return {collection: _record(collection, record_id) for collection, record_id in (
        ("sessions", sid), ("session_runtime", sid),
        ("session_tags", f"{sid}|{sid}"), ("pending_interactions", "p-" + sid))}


def _narrow_pass(worker, session_ids):
    queries = []
    worker.raw.set_trace_callback(queries.append)
    try:
        count = engine.scan_all(collections=set(_catalog_names()), include_catalog=True, strict=True,
                                session_ids=session_ids)
    finally:
        worker.raw.set_trace_callback(None)
    return count, queries


@pytest.fixture
def workers(tmp_path):
    from app.network import tokens
    from tests.core.network.harness import Worker

    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


def test_narrow_catalog_pass_reads_and_publishes_only_dirty_sessions(workers):
    worker = workers("narrow-catalog")
    with worker.active():
        engine.create_network("Narrow catalog")
        for sid in ("dirty", "other"):
            _seed_session(sid)
        engine.scan_all(strict=True)
        before = {sid: _records(sid) for sid in ("dirty", "other")}
        # Unhooked edits to both sessions; only "dirty" is named by the wake.
        for sid in ("dirty", "other"):
            worker.sql("UPDATE sessions SET title = 'Edited' WHERE id = ?", (sid,))
            worker.sql("UPDATE pending_interactions SET state = 'answered' WHERE id = ?", ("p-" + sid,))
            worker.sql("DELETE FROM session_tags WHERE session_id = ?", (sid,))
        worker.sql("INSERT INTO session_tags(session_id,tag_id) VALUES ('dirty','other')")
        count, queries = _narrow_pass(worker, ["dirty"])
        # Session row, prompt, runtime (its question was answered), the new
        # tag, and the removed tag's retraction.
        assert count == 5
        after = {sid: _records(sid) for sid in ("dirty", "other")}
        assert after["other"] == before["other"]
        assert after["dirty"].pop("session_tags")["deleted"]
        assert all(after["dirty"][name]["content_hash"] != before["dirty"][name]["content_hash"]
                   for name in after["dirty"])
        assert not _record("session_tags", "dirty|other")["deleted"]
        # The trace shows bound values: nothing about "other" was read, and no
        # catalog record set was loaded whole.
        assert queries and not [sql for sql in queries if "other" in sql and "dirty" not in sql]
        assert not [sql for sql in queries if "FROM sync_records WHERE collection =" in sql
                    and " AND " not in sql]

        # The periodic whole pass still finds the external edits to "other".
        engine.scan_all(strict=True)
        final = _records("other")
        assert final.pop("session_tags")["deleted"]
        assert all(final[name]["content_hash"] != before["other"][name]["content_hash"] for name in final)


def test_narrow_tag_retraction_does_not_match_similar_ids(workers):
    worker = workers("narrow-like")
    with worker.active():
        engine.create_network("Narrow wildcard ids")
        for sid in ("a_b", "axb", "a%b"):
            _seed_session(sid)
        engine.scan_all(strict=True)
        worker.sql("DELETE FROM session_tags")
        _narrow_pass(worker, ["a_b"])
        assert _record("session_tags", "a_b|a_b")["deleted"]
        assert not _record("session_tags", "axb|axb")["deleted"]
        assert not _record("session_tags", "a%b|a%b")["deleted"]


def test_deleted_dirty_session_is_retracted_like_a_whole_pass(workers):
    from app.db import database
    from app.network import catalog

    worker = workers("narrow-delete")
    with worker.active():
        engine.create_network("Narrow deletion")
        _seed_session("gone")
        _seed_session("fork", parent="gone")
        _seed_session("kept")
        engine.scan_all(strict=True)
        fork_before, kept_before = _records("fork"), _records("kept")
        database.delete_session("gone")
        _narrow_pass(worker, ["gone"])
        assert all(record["deleted"] for record in _records("gone").values())
        # Deleting a session unlinks its forks; that row change is published too.
        fork = _records("fork")
        assert fork.pop("sessions")["content_hash"] != fork_before.pop("sessions")["content_hash"]
        assert fork == fork_before
        assert _records("kept") == kept_before
        assert catalog.publish_owned() == 0


def test_narrow_passes_converge_between_two_workers(workers):
    from app.db import database
    from tests.core.network.harness import converge, join

    a, b = workers("narrow-a"), workers("narrow-b")
    with a.active():
        engine.create_network("Narrow convergence")
    join(a, b)
    for worker, sids in ((a, ("a-live", "a-gone")), (b, ("b-peer",))):
        with worker.active():
            for sid in sids:
                _seed_session(sid)
            engine.scan_all(strict=True)
    converge([a, b])
    with a.active():
        database.update_session("a-live", title="Renamed on A")
        a.sql("INSERT INTO session_tags(session_id,tag_id) VALUES ('a-live','a-gone')")
        _narrow_pass(a, ["a-live"])
        database.delete_session("a-gone")
        _narrow_pass(a, ["a-gone"])
        # A peer's session named by a local wake is left to its owner.
        peer_before = _records("b-peer")
        assert peer_before["sessions"]["origin_device_id"] == b.device_id
        _narrow_pass(a, ["b-peer"])
        assert _records("b-peer") == peer_before
    converge([a, b])
    with b.active():
        assert database.get_session("a-live")["title"] == "Renamed on A"
        assert database.get_session("a-gone") is None
        assert database.get_session("b-peer") is not None
        tags = b.sql("SELECT tag_id FROM session_tags WHERE session_id = 'a-live' ORDER BY tag_id")
        assert [row["tag_id"] for row in tags] == ["a-gone", "a-live"]
        assert _record("pending_interactions", "p-a-gone")["deleted"]
        assert not _record("pending_interactions", "p-b-peer")["deleted"]
        assert not _record("sessions", "b-peer")["deleted"]
