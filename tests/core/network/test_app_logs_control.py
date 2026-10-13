"""GetAppErrors / GetAppLogs ``worker``: another worker's log over the signed control channel (#1787)."""
import asyncio
from unittest.mock import patch

import pytest

from app.db import error_logs
from app.mcp import builtin_tools
from app.network import control, transport
from tests.core.network.test_remote_task_control import UNRAID_URL, network
from tests.mcp.tool_helpers import tool_result_json


def _call(tool, args, *, is_admin=True):
    import app.mcp.inproc as sdk

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id="caller", is_admin=is_admin)
    handler = {t.name: t for t in captured["tools"]}[tool].handler
    return tool_result_json(asyncio.run(handler(args)))


def _control_posts(router):
    return [path for method, path in router.requests(UNRAID_URL) if path.startswith("/api/v1/network/peer/control/")]


def test_errors_come_from_the_named_worker_and_say_so(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    with a.active():
        error_logs.record_error("backend", "unraid database exploded", level="ERROR")
    with b.active():
        error_logs.record_error("backend", "windows only problem", level="ERROR")
        body = _call("GetAppErrors", {"worker": "ServerWorker", "limit": 5})
    assert body["success"] is True, body
    assert body["worker"] == "ServerWorker"
    assert [row["message"] for row in body["errors"]] == ["unraid database exploded"]
    assert _control_posts(router) == ["/api/v1/network/peer/control/app-logs"]


def test_logs_come_from_the_named_worker_with_local_caps(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        body = _call("GetAppLogs", {"worker": a.device_id, "limit": 10_000})
    assert body["success"] is True, body
    assert body["worker"] == "ServerWorker"
    assert body["count"] <= builtin_tools._APP_LOGS_MAX_LIMIT
    assert _control_posts(router) == ["/api/v1/network/peer/control/app-logs"]


def test_naming_this_worker_reads_here_and_says_so(workers, monkeypatch):
    _a, b, router = network(workers, monkeypatch)
    with b.active():
        error_logs.record_error("backend", "windows only problem", level="ERROR")
        body = _call("GetAppErrors", {"worker": "windows"})
    assert body["worker"] == "Windows"
    assert [row["message"] for row in body["errors"]] == ["windows only problem"]
    assert _control_posts(router) == []


@pytest.mark.parametrize("worker, expected", [
    ("Nowhere", "Unknown worker 'Nowhere'"),
    ("ServerWorker", "ServerWorker is offline; nothing was read"),
])
def test_an_unknown_or_offline_worker_is_refused_by_name(workers, monkeypatch, worker, expected):
    _a, b, router = network(workers, monkeypatch)
    monkeypatch.setattr(transport, "online_devices", dict)
    with b.active():
        error_logs.record_error("backend", "windows only problem", level="ERROR")
        for tool in ("GetAppErrors", "GetAppLogs"):
            body = _call(tool, {"worker": worker})
            assert body["success"] is False
            assert expected in body["error"]
            assert "errors" not in body and "lines" not in body  # never answered from here
    assert _control_posts(router) == []


def test_an_older_worker_is_refused_as_older(workers, monkeypatch):
    _a, b, _ = network(workers, monkeypatch)
    monkeypatch.setattr(control, "known_action", lambda action: action != builtin_tools.APP_LOGS_ACTION)
    with b.active():
        body = _call("GetAppLogs", {"worker": "ServerWorker"})
    assert body["success"] is False
    assert body["error"].startswith("ServerWorker runs an older release that cannot share its logs")
    assert body["worker"] == "ServerWorker"


def test_a_peer_refusal_comes_back_by_name(workers, monkeypatch):
    _a, b, _ = network(workers, monkeypatch)
    with b.active():
        body = _call("GetAppLogs", {"worker": "ServerWorker", "level": "LOUD"})
    assert body["success"] is False
    assert body["error"].startswith("ServerWorker refused the read: level must be one of")


def test_an_api_key_session_is_refused(workers, monkeypatch):
    _a, b, router = network(workers, monkeypatch)
    with b.active():
        for tool in ("GetAppErrors", "GetAppLogs"):
            body = _call(tool, {"worker": "ServerWorker"}, is_admin=False)
            assert body["success"] is False and "requires an owner session" in body["error"]
    assert _control_posts(router) == []


def test_the_peer_answers_only_its_own_workers(workers, monkeypatch):
    a, b, _ = network(workers, monkeypatch)
    with a.active():
        with pytest.raises(PermissionError):
            asyncio.run(builtin_tools.handle_peer_app_logs({"kind": "logs", "args": {}}, "dev_stranger"))
        with pytest.raises(ValueError):
            asyncio.run(builtin_tools.handle_peer_app_logs({"kind": "files", "args": {}}, b.device_id))
        answer = asyncio.run(builtin_tools.handle_peer_app_logs(
            {"kind": "logs", "args": {"limit": 1, "worker": "Windows"}}, b.device_id))
    assert set(answer) >= {"lines", "count", "matched", "buffered"}
    assert "worker" not in answer
