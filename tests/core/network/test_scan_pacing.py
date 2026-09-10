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
        monkeypatch.setattr(transport, "_loop", SimpleNamespace(
            call_soon_threadsafe=lambda callback: callback()))
        monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
            Event=asyncio.Event, CancelledError=asyncio.CancelledError,
            TimeoutError=asyncio.TimeoutError, sleep=self.sleep,
            wait_for=self.wait_for, to_thread=self.to_thread))

    async def to_thread(self, fn):
        if len(self.scans) == self.passes:
            raise asyncio.CancelledError
        self.scans.append(self.now)
        return fn()

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
            monkeypatch.setattr(engine, "scan_all", lambda: engine.scan_collection("agents"))
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
