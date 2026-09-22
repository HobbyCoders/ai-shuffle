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
            for sid in ("busy", "changed"):
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
