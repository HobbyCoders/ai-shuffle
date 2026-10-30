"""Conservative browser health policy and non-runtime owner-use leases."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest
from app.core import native_chrome as chrome, browser_runtime as runtime, browser_control as control


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(chrome, "_health_states", {})
    monkeypatch.setattr(chrome, "_docker_restarts", [])
    monkeypatch.setattr(chrome, "_protected_pages", set())
    monkeypatch.setattr(chrome, "_external_users", 0)
    monkeypatch.setattr(chrome, "_recovery_done", None)
    monkeypatch.setattr(control, "_brokers", {})
    monkeypatch.setattr(runtime, "_runtimes", {})


@pytest.mark.asyncio
async def test_cold_start_http_failures_never_restart_and_warm_failures_need_thirty_seconds(monkeypatch):
    clock = [0]
    monkeypatch.setattr(chrome, "_health_now", lambda: clock[0])
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(chrome, "cdp_get", get)
    for clock[0] in (0, 20, 40, 80):
        assert await chrome._probe_pages(1234) is None
    get.side_effect = [{"webSocketDebuggerUrl": "ws://127.0.0.1/browser"}, []]
    assert await chrome._probe_pages(1234) is None
    get.side_effect = None
    for clock[0] in (100, 115, 129):
        assert await chrome._probe_pages(1234) is None
    clock[0] = 131
    assert await chrome._probe_pages(1234) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("docker", [False, True])
async def test_both_supervisors_share_restart_limits_and_back_off_without_blocking_admission(monkeypatch, docker):
    clock = [100]
    monkeypatch.setattr(chrome, "_health_now", lambda: clock[0])
    probe = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(chrome, "_probe_pages", probe)
    process = Mock(pid=123)
    process.create_time.return_value = 1
    monkeypatch.setattr(chrome, "_docker_chrome_process", lambda port: process)
    stop = AsyncMock()
    monkeypatch.setattr(chrome, "_stop_process", stop)
    for clock[0] in (100, 161, 222):
        await chrome._check_health(1234, docker=docker)
    assert (process.terminate.call_count if docker else stop.await_count) == 3
    before = probe.await_count
    clock[0] = 230
    await chrome._check_health(1234, docker=docker)
    assert probe.await_count == before
    assert chrome._recovery_done is None
    await asyncio.wait_for(chrome.wait_for_recovery(), .1)


@pytest.mark.asyncio
async def test_docker_termination_is_graceful_before_forcing_only_same_process(monkeypatch):
    process = Mock(pid=123)
    process.create_time.return_value = 1
    process.wait.side_effect = psutil.TimeoutExpired(5)
    monkeypatch.setattr(chrome, "_docker_chrome_process", lambda port: process)
    assert await chrome._restart_docker_chrome(1234, process)
    assert [call[0] for call in process.method_calls] == ["create_time", "create_time", "terminate", "wait", "kill"]
    process.wait.assert_called_once_with(timeout=5)


@pytest.mark.asyncio
async def test_probing_does_not_hold_admission_and_new_activity_cancels_repair(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def probe(port):
        entered.set()
        await finish.wait()
        return None, None

    monkeypatch.setattr(chrome, "_probe_pages", probe)
    stop = AsyncMock()
    monkeypatch.setattr(chrome, "_stop_process", stop)
    job = asyncio.create_task(chrome._check_health(1234))
    await entered.wait()
    await asyncio.wait_for(chrome.wait_for_recovery(), .1)
    runtime._runtimes["new-action"] = SimpleNamespace(active=1)
    finish.set()
    await job
    stop.assert_not_awaited()

@pytest.mark.asyncio
async def test_billing_tab_is_protected_until_closed(monkeypatch):
    async def create(*args):
        assert chrome._external_users == 1
        return {"id": "billing"}

    monkeypatch.setattr(chrome, "_create_page", create)
    await chrome.create_page("https://example.test/billing")
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(chrome, "_probe_pages", probe)
    get = AsyncMock(return_value=[{"id": "billing"}])
    monkeypatch.setattr(chrome, "cdp_get", get)
    await chrome._check_health(chrome._cdp_port())
    probe.assert_not_awaited()
    get.return_value = []
    await chrome._check_health(chrome._cdp_port())
    probe.assert_awaited_once()
    assert not chrome._protected_pages


@pytest.mark.asyncio
async def test_pending_dialog_status_or_unknown_status_blocks_recovery():
    client = SimpleNamespace(call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": json.dumps({"pending_dialog": {"type": "confirm"}})}]}))
    runtime._runtimes["dialog"] = SimpleNamespace(active=0, client=client)
    assert await runtime.dialogs_block_recovery()
    client.call_tool.side_effect = ConnectionError()
    assert await runtime.dialogs_block_recovery()
