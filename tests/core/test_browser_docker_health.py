"""Docker page recovery uses isolated sockets/mocked PIDs, never live Chrome."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from app.core import native_chrome as chrome, browser_control as control, browser_runtime as runtime


@pytest.fixture(autouse=True)
def clean_recovery(monkeypatch):
    monkeypatch.setattr(chrome, "_recovery_done", None)
    monkeypatch.setattr(chrome, "_docker_restarts", [])
    monkeypatch.setattr(chrome, "_health_states", {})
    monkeypatch.setattr(chrome, "_protected_pages", set())
    monkeypatch.setattr(chrome, "_external_users", 0)
    monkeypatch.setattr(control, "_brokers", {})
    monkeypatch.setattr(runtime, "_runtimes", {})


@pytest.mark.parametrize("reason", ["human", "pending_viewer", "queued", "reserved", "busy", "agent"])
@pytest.mark.asyncio
async def test_recovery_defers_while_any_session_is_using_shared_browser(monkeypatch, reason):
    broker = control.get("other-session")
    if reason == "human":
        broker.state["control"].update(owner="human", viewer_id="owner")
    elif reason == "pending_viewer":
        broker.state["control"]["pending_viewer_id"] = "owner"
    elif reason == "queued":
        broker.pending["command"] = {}
    elif reason == "reserved":
        broker.reserved_viewer = "owner"
    elif reason == "busy":
        broker.state["busy"] = True
    else:
        runtime._runtimes["agent-session"] = SimpleNamespace(active=1)
    probe = AsyncMock()
    owner = Mock()
    monkeypatch.setattr(chrome, "_probe_pages", probe)
    monkeypatch.setattr(chrome, "_docker_chrome_process", owner)
    await chrome._check_health(9221, docker=True)
    probe.assert_not_awaited()
    owner.assert_not_called()


@pytest.mark.asyncio
async def test_new_agent_calls_and_takeovers_wait_until_repair_finishes(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def probe(*args):
        started.set()
        await finish.wait()
        return True

    monkeypatch.setattr(chrome, "_probe_pages", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(chrome, "_repair_pages", probe)
    monkeypatch.setattr(chrome, "_docker_chrome_process", Mock(return_value=Mock(pid=123)))
    repair = asyncio.create_task(chrome._check_health(9221, docker=True))
    await started.wait()
    broker = control.get("session")
    await broker.register("runtime")
    broker.state.update(runtime_id="runtime", active_tab_id="tab", available=True)
    takeover = asyncio.create_task(broker.submit("viewer", dict(
        id="take", runtime_id="runtime", tab_id="tab", op="takeover", args={},
    )))
    actor = runtime.get("agent-session")
    actor.ensure = AsyncMock()
    actor.client = SimpleNamespace(call_tool=AsyncMock(return_value={}))
    action = asyncio.create_task(actor.call("browser_eval", {}))
    try:
        await asyncio.sleep(0)
        assert not broker.pending and actor.active == 0
        actor.client.call_tool.assert_not_awaited()
        finish.set()
        await repair
        await takeover
        await action
        assert broker.pending["take"]
        actor.client.call_tool.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(repair, takeover, action, return_exceptions=True)
        broker.close()
        actor.idle.cancel()
        await asyncio.gather(actor.idle, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_health_check_releases_admission_barrier(monkeypatch):
    started = asyncio.Event()

    async def probe(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chrome, "_probe_pages", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(chrome, "_repair_pages", probe)
    monkeypatch.setattr(chrome, "_docker_chrome_process", Mock(return_value=Mock(pid=123)))
    repair = asyncio.create_task(chrome._check_health(9221, docker=True))
    await started.wait()
    repair.cancel()
    await asyncio.gather(repair, return_exceptions=True)
    await asyncio.wait_for(chrome.wait_for_recovery(), 0.1)
    assert chrome._recovery_done is None


@pytest.mark.asyncio
async def test_unknown_docker_listener_is_not_probed_or_restarted(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(chrome, "_docker_chrome_process", lambda port: None)
    monkeypatch.setattr(chrome, "_probe_pages", probe)
    await chrome._check_health(9221, docker=True)
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_restart_is_rate_limited_and_reverifies_pid_creation(monkeypatch):
    process = Mock(pid=123)
    process.create_time.return_value = 10
    monkeypatch.setattr(chrome, "_docker_chrome_process", lambda port: process)
    clock = Mock(return_value=100)
    monkeypatch.setattr(chrome, "_health_now", clock)
    assert await chrome._restart_docker_chrome(9221, process)
    assert not await chrome._restart_docker_chrome(9221, process)
    for now in [161, 222]:
        clock.return_value = now
        assert await chrome._restart_docker_chrome(9221, process)
    clock.return_value = 283
    assert not await chrome._restart_docker_chrome(9221, process)
    assert process.terminate.call_count == 3
    process.kill.assert_not_called()
    clock.return_value = 900
    changed = Mock(pid=123)
    changed.create_time.return_value = 20
    monkeypatch.setattr(chrome, "_docker_chrome_process", lambda port: changed)
    assert not await chrome._restart_docker_chrome(9221, process)
    changed.kill.assert_not_called()


@pytest.mark.parametrize("mismatch", [None, "supervisor", "egress", "uid", "profile", "port", "renderer", "exe", "ambiguous", "unknown", "denied"])
def test_docker_pid_selection_is_narrow_and_fails_closed(monkeypatch, mismatch):
    process = Mock(pid=123)
    process.uids.return_value = SimpleNamespace(real=1000)
    process.exe.return_value = "/usr/lib/chromium/chromium" if mismatch != "exe" else "/usr/bin/node"
    args = ["chromium", "--remote-debugging-port=9221", f"--user-data-dir={Path.home() / 'chrome-profile'}"]
    if mismatch == "profile":
        args[-1] = "--user-data-dir=/unrelated/profile"
    if mismatch == "port":
        args[1] = "--remote-debugging-port=9999"
    if mismatch == "renderer":
        args.append("--type=renderer")
    process.cmdline.return_value = args
    parent = Mock()
    parent.cmdline.return_value = ["node", "/opt/browser-mcp/dist/egress-launch.js"] if mismatch != "egress" else ["unrelated"]
    supervisor = Mock()
    supervisor.cmdline.return_value = ["bash", "/usr/local/bin/chrome-supervisor"] if mismatch != "supervisor" else ["external"]
    process.parents.return_value = [parent, supervisor]
    monkeypatch.setattr(chrome.os, "getuid", lambda: 1001 if mismatch == "uid" else 1000, raising=False)
    monkeypatch.setattr(psutil, "Process", Mock(return_value=process))
    listeners = [SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9221), pid=123)]
    if mismatch == "ambiguous":
        listeners.append(SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9221), pid=456))
    if mismatch == "unknown":
        listeners[0].pid = None
    connections = Mock(return_value=listeners)
    if mismatch == "denied":
        connections.side_effect = psutil.AccessDenied()
    monkeypatch.setattr(psutil, "net_connections", connections)
    assert chrome._docker_chrome_process(9221) is (process if mismatch is None else None)
    process.kill.assert_not_called()
