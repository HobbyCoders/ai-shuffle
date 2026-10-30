"""Availability uses the install that a real signed peer presence actually names."""
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from app.core import worker_availability, worker_settings as ws
from app.core.config import settings
from app.db import database
from app.network import control, engine, transport
from tests.core.network.harness import Worker, exchange, join


@pytest.fixture(autouse=True)
def isolated_settings_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    ws._readings.clear()
    monkeypatch.setattr(ws, "local_status", AsyncMock(return_value=ws.MachineStatus().model_dump()))
    monkeypatch.setattr(transport, "online_devices", lambda: set())
    monkeypatch.setattr(control, "call", AsyncMock())
    yield
    ws._readings.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, "inst-old"])
async def test_signed_current_install_scopes_availability_reads_and_writes(
    workers: Callable[[str], Worker], legacy: str | None,
) -> None:
    a, b = workers("a"), workers("b")
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        engine.create_network("Test", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id=legacy)
    with b.active(), patch.object(settings, "instance_id", "inst-current"):
        engine.publish_presence()
    assert exchange(b, a).applied >= 1

    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        database.set_system_setting("worker_always_on:inst-old", "false")
        result = await ws.dispatch(ws.Dispatch(
            target=b.device_id, action={"operation": "always-on", "always_on": True}))
        assert result["workers"][0]["outcome"] == "accepted"
        assert database.get_system_setting("worker_always_on:inst-current") == "true"
        assert database.get_system_setting("worker_always_on:inst-old") == "false"
        assert database.get_system_setting("worker_always_on:" + b.device_id) is None
        peer = next(row for row in (await ws.snapshot())["workers"]
                    if row["device_id"] == b.device_id)
        assert peer["availability"] == {
            "editable": True, "always_on": True, "always_on_override": True}
        engine.scan_all()
    assert exchange(a, b).applied >= 1
    with b.active(), patch.object(settings, "instance_id", "inst-current"):
        assert database.get_system_setting(worker_availability.setting_key()) == "true"
        assert worker_availability.snapshot()["always_on_override"] is True
    control.call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_is_device_id", [False, True])
async def test_worker_without_install_claim_is_not_editable(
    workers: Callable[[str], Worker], legacy_is_device_id: bool,
) -> None:
    a, b = workers("a"), workers("b")
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        engine.create_network("Test", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id=b.device_id if legacy_is_device_id else None)
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        assert engine.instance_aliases()[b.device_id] == b.device_id
        peer = next(row for row in (await ws.snapshot())["workers"]
                    if row["device_id"] == b.device_id)
        assert peer["availability"]["editable"] is False
        result = await ws.dispatch(ws.Dispatch(
            target=b.device_id, action={"operation": "always-on", "always_on": False}))
        assert result["workers"][0]["reason"] == "unknown-instance"
        assert database.get_system_setting("worker_always_on:" + b.device_id) is None
        assert database.get_system_setting(worker_availability.setting_key()) is None
    control.call.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_current_claim_cannot_fall_back_to_old_certificate(
    workers: Callable[[str], Worker],
) -> None:
    a, b = workers("a"), workers("b")
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        engine.create_network("Test", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id="inst-old")
    with b.active(), patch.object(settings, "instance_id", "inst-a"):
        engine.publish_presence()
    assert exchange(b, a).applied >= 1
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        assert engine.instance_aliases()["inst-a"] == a.device_id
        result = await ws.dispatch(ws.Dispatch(
            target=b.device_id, action={"operation": "always-on", "always_on": False}))
        assert result["workers"][0]["reason"] == "unknown-instance"
        assert database.get_system_setting("worker_always_on:inst-old") is None
        assert database.get_system_setting("worker_always_on:inst-a") is None
    control.call.assert_not_called()


def test_certificate_install_claim_is_used_until_current_presence_arrives(
    workers: Callable[[str], Worker],
) -> None:
    a, b = workers("a"), workers("b")
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        engine.create_network("Test", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id="inst-b")
    with a.active(), patch.object(settings, "instance_id", "inst-a"):
        peer = next(row for row in ws._directory() if row["device_id"] == b.device_id)
        assert ws._instance(peer) == "inst-b"
