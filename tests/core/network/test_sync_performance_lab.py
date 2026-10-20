"""Production sync/scan loops on a deterministic virtual clock, not a LAN timing gate."""
import json
from collections import Counter
from time import perf_counter, process_time

from app.db import session_events
from app.network import engine, transport
from tests.core.network.test_sync_cadence import peers  # noqa: F401 - shared two-worker fixture


async def test_message_propagation_latency_and_idle_cost(peers, monkeypatch):
    latencies = []
    written = {}
    real_notify = engine._notify
    def notify(touched):
        real_notify(touched)
        if engine.require_context().device_id != peers.b.device_id:
            return
        for seq in list(written):
            rows = peers.b.sql("SELECT seq FROM session_events WHERE session_id=? AND seq=?", ("a-0", seq))
            if rows:
                latencies.append(peers.clock.now - written.pop(seq))

    monkeypatch.setattr(engine, "_notify", notify)

    async def messages():
        # Uneven spacing exercises multiple phases of the scanner's wake timer.
        for n in range(30):
            await peers.clock.sleep(7.1 + (n % 5) * 0.13)
            with peers.a.active():
                first, _ = session_events.append_events("a-0", [
                    {"type": "message.user", "payload": {"type": "message.user", "content": f"probe {n}"}}])
                written[first] = peers.clock.now
            peers.na.module.request_scan(["session_events"], session_ids=["a-0"])
        await peers.clock.sleep(10)

    await peers.run(300, during=messages)
    assert not written and len(latencies) == 30
    assert max(latencies) <= transport.SCAN_INTERVAL_SECONDS
    ordered = sorted(latencies)
    print("PROPAGATION_METRICS " + json.dumps({
        "clock": "virtual_seconds", "samples": len(ordered),
        "p50": ordered[14], "p95": ordered[28], "max": ordered[-1]}))


async def test_idle_sync_lab_request_and_database_budget(peers):
    counts = Counter()
    statements = []
    for worker in (peers.a, peers.b):
        worker.raw.set_trace_callback(statements.append)
    cpu_start, wall_start = process_time(), perf_counter()
    try:
        await peers.run(900)
    finally:
        for worker in (peers.a, peers.b):
            worker.raw.set_trace_callback(None)
    for node in (peers.na, peers.nb):
        counts.update(path for _, path in node.sent)
    # Existing production cadence: 32 exchanges per peer over 15 virtual
    # minutes. No outbound operation pushes at idle.
    assert counts["/api/v1/network/peer/exchange"] == 64
    assert counts["/api/v1/network/peer/ops"] == 0
    print("IDLE_METRICS " + json.dumps({
        "virtual_seconds": 900, "requests": dict(counts), "sqlite_statements": len(statements),
        "lab_cpu_seconds": process_time() - cpu_start,
        "lab_wall_seconds": perf_counter() - wall_start}))
