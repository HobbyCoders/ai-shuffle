"""Peer latency attribution is bounded, sanitized and independent of route behavior."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import network as api
from app.network import control, request_metrics, transport
from tests.core.network.harness import owner_pref, set_owner_pref
from tests.core.network.test_lan_direct import LAN_A, TUNNEL_A, Net, _pair, addresses  # noqa: F401


@pytest.fixture
def metrics(monkeypatch):
    now = [0.0]
    result = request_metrics.RequestMetrics(clock=lambda: now[0])
    monkeypatch.setattr(request_metrics, "metrics", result)
    yield result, now
    result.flush()




async def test_record_cost_lab(metrics):
    from time import perf_counter, process_time
    measured, _ = metrics
    count = 10000
    wall, cpu = perf_counter(), process_time()
    for _ in range(count):
        measured.record("control", "session-git", 200, .001)
    print("PEER_METRIC_COST " + json.dumps({
        "calls": count, "wall_ms": (perf_counter() - wall) * 1000,
        "cpu_ms": (process_time() - cpu) * 1000,
        "samples": len(measured._buckets[("control", "session-git")].samples)}))
    assert measured._buckets[("control", "session-git")].count == count
    assert len(measured._buckets[("control", "session-git")].samples) == request_metrics.MAX_SAMPLES


def summary(caplog):
    record = next(row for row in reversed(caplog.records)
                  if row.name == request_metrics.__name__ and "summary" in row.message)
    return json.loads(record.message.split("endpoints=", 1)[1])


async def test_minute_summary_counts_errors_and_nearest_rank_quantiles(metrics, caplog, monkeypatch):
    measured, now = metrics
    caplog.set_level(logging.DEBUG, logger=request_metrics.__name__)
    scheduled = []
    timer = SimpleNamespace(cancel=lambda: None)
    loop = SimpleNamespace(is_closed=lambda: False,
                           call_later=lambda delay, callback: scheduled.append((delay, callback)) or timer)
    monkeypatch.setattr(request_metrics, "asyncio", SimpleNamespace(get_running_loop=lambda: loop))
    for n in range(1, 101):
        measured.record("control", "session-git", 500 if n % 10 == 0 else 200, n / 1000)
    assert len(scheduled) == 1 and scheduled[0][0] == 60.0
    assert len([r for r in caplog.records if r.levelno == logging.DEBUG]) == 100
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
    now[0] = 60
    scheduled[0][1]()
    assert summary(caplog) == [{"transport": "control", "endpoint": "session-git", "count": 100,
                                "errors": 10, "sample_count": 100, "p50_ms": 50, "p95_ms": 95}]
    assert "window_s=60.000" in caplog.records[-1].message
    measured.flush()
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
    assert not measured._buckets and measured._timer is None


async def test_cardinality_and_quantile_memory_are_bounded(metrics, caplog):
    measured, _ = metrics
    caplog.set_level(logging.INFO, logger=request_metrics.__name__)
    for n in range(request_metrics.MAX_SAMPLES * 3):
        measured.record("control", "session-git", 200, n / 1000)
    for n in range(request_metrics.MAX_ENDPOINTS * 3):
        measured.record("control", f"registered-action-{n}", 200, .001)
    assert len(measured._buckets) <= request_metrics.MAX_ENDPOINTS
    assert max(len(b.samples) for b in measured._buckets.values()) <= request_metrics.MAX_SAMPLES
    measured.flush()
    entries = summary(caplog)
    entry = next(e for e in entries if e["endpoint"] == "session-git")
    assert entry["count"] == request_metrics.MAX_SAMPLES * 3
    assert entry["sample_count"] == request_metrics.MAX_SAMPLES
    assert sum(e["count"] for e in entries) == request_metrics.MAX_SAMPLES * 3 + request_metrics.MAX_ENDPOINTS * 3
    assert any(e["endpoint"] == "other" for e in entries)


@pytest.mark.parametrize("path,expected", [
    ("/api/v1/network/peer/exchange", "POST /peer/exchange"),
    ("/api/v1/network/peer/ops/private-device/1/2", "GET /peer/ops/{param}/{param}/{param}"),
    ("/api/v1/network/peer/state/private-snapshot/4", "GET /peer/state/{param}/{param}"),
    ("/api/v1/network/peer/control/session-git", "unmatched"),
    ("/private-secret?password=do-not-log", "unmatched"),
])
def test_lan_route_labels_never_contain_dynamic_values(path, expected):
    assert api._lan_metric_endpoint({"m": "POST" if path.endswith("exchange") else "GET", "p": path}) == expected


@pytest.mark.parametrize("failure,status", [
    (None, 200), (PermissionError("private body"), 409),
    (LookupError("private id"), 404), (HTTPException(503, "private address"), 503),
])
async def test_control_boundary_preserves_status_and_omits_private_data(metrics, monkeypatch, caplog, failure, status):
    measured, _ = metrics
    caplog.set_level(logging.DEBUG, logger=request_metrics.__name__)
    fake_time = iter([100.0, 102.0])
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: next(fake_time)))
    monkeypatch.setattr(transport, "note_contact", lambda device: None)
    handler = AsyncMock(return_value={"success": True}, side_effect=failure)
    monkeypatch.setattr(control, "handle", handler)
    request = SimpleNamespace(json=AsyncMock(return_value={"session_id": "private-session", "secret": "private-input"}),
                              url=SimpleNamespace(scheme="https"), scope={})
    if failure is None:
        assert await api.peer_control("session-git", request, "private-device") == {"success": True}
    else:
        with pytest.raises(HTTPException) as caught:
            await api.peer_control("session-git", request, "private-device")
        assert caught.value.status_code == status
    measured.flush()
    entry = summary(caplog)[0]
    assert entry["endpoint"] == "session-git" and entry["p50_ms"] == 2000
    assert entry["errors"] == int(status >= 400)
    telemetry = " ".join(r.message for r in caplog.records if r.name == request_metrics.__name__)
    assert f"status={status}" in telemetry and "private-" not in telemetry
    assert not control.inbound_https.get()


async def test_real_sealed_lan_exchange_and_direct_controls_are_separate(workers, addresses, metrics, caplog):
    from app.network import engine
    measured, _ = metrics
    caplog.set_level(logging.INFO, logger=request_metrics.__name__)
    a, b = _pair(workers, addresses)
    set_owner_pref(a, "telemetry-pref", "private-value")
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.require_context())
    assert owner_pref(b, "telemetry-pref") == "private-value"
    measured.flush()
    entries = summary(caplog)
    assert any(e["transport"] == "lan" and e["endpoint"] == "POST /peer/exchange" for e in entries)
    assert all(e["transport"] == "lan" for e in entries)
    telemetry = " ".join(r.message for r in caplog.records if r.name == request_metrics.__name__)
    for private in (a.device_id, b.device_id, "private-value", LAN_A, TUNNEL_A):
        assert private not in telemetry
