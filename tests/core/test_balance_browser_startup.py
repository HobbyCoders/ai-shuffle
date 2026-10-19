"""Private sign-in only attaches to the worker's supervised browser."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from app.core import native_chrome as chrome
from app.core.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("docker", [False, True])
async def test_signin_waits_for_browser_before_connecting(monkeypatch, docker):
    monkeypatch.setattr(settings, "start_chrome", True)
    task = Mock(done=Mock(return_value=False))
    monkeypatch.setattr(chrome, "_supervisor_task", None if docker else task)
    monkeypatch.setattr(chrome, "_in_container", lambda: docker)
    monkeypatch.setattr(chrome, "ensure_started", AsyncMock())
    probe = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr(chrome, "_cdp_answers", probe)
    monkeypatch.setattr(chrome, "_owns_cdp_port", Mock(return_value=True))
    version = {"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/test"}
    monkeypatch.setattr(chrome, "cdp_get", AsyncMock(return_value=version))
    assert await chrome.sign_in_browser_version() == version
    assert probe.await_count == 3
    chrome.cdp_get.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case, message", [("disabled", "disabled"), ("external", "external"),
    ("no_owner", "not owned"), ("timeout", "timed out"), ("stopped", "stopped"), ("disconnect", "disconnected")])
async def test_signin_startup_fails_closed(monkeypatch, case, message):
    monkeypatch.setattr(settings, "start_chrome", case != "disabled")
    monkeypatch.setattr(chrome, "_in_container", lambda: False)
    monkeypatch.setattr(chrome, "_supervisor_task", None if case == "external" else Mock(done=Mock(return_value=False)))
    monkeypatch.setattr(chrome, "ensure_started", AsyncMock())
    monkeypatch.setattr(chrome, "wait_until_answering", AsyncMock(return_value=case != "stopped",
        side_effect=TimeoutError if case == "timeout" else None))
    monkeypatch.setattr(chrome, "_owns_cdp_port", Mock(return_value=case != "no_owner"))
    monkeypatch.setattr(chrome, "cdp_get", AsyncMock(return_value=None))
    with pytest.raises(chrome.BrowserSignInUnavailable, match=message):
        await chrome.sign_in_browser_version()
    if case == "disabled":
        chrome.ensure_started.assert_not_awaited()
    if case != "disconnect":
        chrome.cdp_get.assert_not_awaited()


@pytest.mark.parametrize("supervised, egress, allowed", [
    (True, True, True), (False, True, False), (True, False, False),
])
def test_port_ownership_requires_supervised_listener(monkeypatch, supervised, egress, allowed):
    monkeypatch.setattr(chrome, "_in_container", lambda: True)
    monkeypatch.setattr(chrome, "_proc", SimpleNamespace(pid=20, returncode=None))
    parent = Mock(pid=20 if supervised else 99)
    parent.cmdline.return_value = ["node", "/opt/browser-mcp/dist/egress-launch.js"] if egress else ["chrome"]
    supervisor = Mock(pid=10)
    supervisor.cmdline.return_value = ["/bin/bash", "/usr/local/bin/chrome-supervisor"] if supervised else ["external"]
    browser = Mock(pid=30)
    browser.parents.return_value = [parent, supervisor]
    browser.cmdline.return_value = ["chrome"]
    monkeypatch.setattr(psutil, "Process", Mock(return_value=browser))
    listener = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9221), pid=30)
    monkeypatch.setattr(psutil, "net_connections", Mock(return_value=[listener]))
    assert chrome._owns_cdp_port(9221) is allowed
    # An unknown listener is never silently trusted.
    listener.pid = None
    assert not chrome._owns_cdp_port(9221)


@pytest.mark.parametrize("legacy", [False, True])
def test_native_ownership_uses_supervised_process_connections_without_root(monkeypatch, legacy):
    monkeypatch.setattr(chrome, "_in_container", lambda: False)
    monkeypatch.setattr(chrome, "_proc", SimpleNamespace(pid=20, returncode=None))
    system = Mock(side_effect=psutil.AccessDenied())
    monkeypatch.setattr(psutil, "net_connections", system)
    launcher = SimpleNamespace(pid=20, net_connections=Mock(return_value=[]))
    listener = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9221))
    own_connections = Mock(return_value=[listener])
    child = SimpleNamespace(pid=30, **{"connections" if legacy else "net_connections": own_connections})
    launcher.children = Mock(return_value=[child])
    process = Mock(return_value=launcher)
    monkeypatch.setattr(psutil, "Process", process)
    assert chrome._owns_cdp_port(9221)
    process.assert_called_once_with(20)
    launcher.children.assert_called_once_with(recursive=True)
    system.assert_not_called()
    # An external listener is insufficient when no owned child has that port.
    own_connections.return_value = []
    assert not chrome._owns_cdp_port(9221)
    own_connections.side_effect = psutil.AccessDenied()
    assert not chrome._owns_cdp_port(9221)
