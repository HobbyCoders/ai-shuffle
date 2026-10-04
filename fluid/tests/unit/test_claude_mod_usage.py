"""Plan usage from AI Shuffle's Claude Code mod (``claude_mod/hooks/usage.ts``).

The mod posts the rate-limit windows the CLI measured to the delegated
loop's bridge socket as one HTTP request. These tests cover the report's
contract (``claude_mod_usage``), the bridge's HTTP route (auth, local peers
only, bounds, bad bodies, and that its token and the bridge token never
stand in for each other), and the loop's wiring: the CLI's environment
names the route only when the mod is loaded and a host takes the reports.
The mod's side runs under ``claude plugin test`` (``claude_mod/tests``).
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from fluid.engine.claude_mod_usage import (
    MAX_AGE_SECONDS,
    USAGE_PATH,
    PlanUsageReport,
    PlanWindow,
    parse_plan_usage_report,
)
from fluid.engine.delegated import (
    CLAUDE_MOD_SOCKET_ENV,
    CLAUDE_MOD_TOKEN_ENV,
    CLAUDE_MOD_URL_ENV,
    DelegatedLoop,
)
from fluid.events import ErrorEvent, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.store.base import MemoryStore
from fluid.tools.mcp import bundled_server
from fluid.tools.mcp.bundled_server import HTTP_MAX_BODY_BYTES, serve_registry_socket
from fluid.tools.registry import ToolRegistry

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
NOW = 1_800_000_000.0


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "v": 1,
        "measuredAt": NOW * 1000,
        "rateLimits": [
            {"kind": "five_hour", "percentUsed": 23.5, "resetsAt": "2027-01-15T10:00:00Z"},
            {"kind": "seven_day", "percentUsed": 7},
        ],
    }
    body.update(overrides)
    return body


# ─── The report ──────────────────────────────────────────────────────────────


def test_a_report_keeps_kind_share_and_reset() -> None:
    reset = int(datetime(2027, 1, 15, 10, tzinfo=timezone.utc).timestamp())
    assert parse_plan_usage_report(_body(), now=NOW) == PlanUsageReport(measured_at=NOW, windows=(
        PlanWindow(kind="five_hour", percent_used=23.5, resets_at=reset),
        PlanWindow(kind="seven_day", percent_used=7.0, resets_at=None),
    ))


def test_unknown_keys_are_dropped_and_epoch_resets_accepted() -> None:
    body = _body(rateLimits=[
        {"kind": "five_hour", "percentUsed": 10.04, "resetsAt": NOW + 3600, "email": "x@y"},
        {"kind": "spend_limit", "percentUsed": 140, "resetsAt": (NOW + 60) * 1000},
    ], account="someone")
    report = parse_plan_usage_report(body, now=NOW)
    assert report.windows == (
        PlanWindow(kind="five_hour", percent_used=10.0, resets_at=int(NOW + 3600)),
        PlanWindow(kind="spend_limit", percent_used=140.0, resets_at=int(NOW + 60)),
    )


def test_a_reading_stamped_slightly_ahead_is_dated_now() -> None:
    assert parse_plan_usage_report(_body(measuredAt=(NOW + 30) * 1000), now=NOW).measured_at == NOW


@pytest.mark.parametrize("body", [
    None, [], "text", {"v": 2, "measuredAt": NOW * 1000, "rateLimits": [{"kind": "a", "percentUsed": 1}]},
    _body(measuredAt=None), _body(measuredAt="1800000000000"), _body(measuredAt=True),
    _body(measuredAt=(NOW + 120) * 1000), _body(measuredAt=(NOW - MAX_AGE_SECONDS - 1) * 1000),
    _body(rateLimits=[]), _body(rateLimits={"kind": "five_hour"}),
    _body(rateLimits=[{"kind": f"k{i}", "percentUsed": 1} for i in range(9)]),
    _body(rateLimits=["five_hour"]),
    _body(rateLimits=[{"kind": "Five Hour", "percentUsed": 1}]),
    _body(rateLimits=[{"kind": "x" * 41, "percentUsed": 1}]),
    _body(rateLimits=[{"percentUsed": 1}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 1}, {"kind": "a", "percentUsed": 2}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": -1}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 1001}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": "5"}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": float("nan")}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 5, "resetsAt": "soon"}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 5, "resetsAt": "2" * 65}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 5, "resetsAt": NOW + 401 * 86400}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 5, "resetsAt": -5}]),
    _body(rateLimits=[{"kind": "a", "percentUsed": 5, "resetsAt": {"at": 1}}]),
])
def test_a_malformed_or_unbounded_report_is_refused(body) -> None:
    with pytest.raises(ValueError):
        parse_plan_usage_report(body, now=NOW)


# ─── The bridge's HTTP route ─────────────────────────────────────────────────


HTTP_TOKEN = "http-route-token"
BRIDGE_TOKEN = "bridge-tool-token"


async def _served(tmp_path: Path, route: Any) -> tuple[Any, str]:
    use_unix = hasattr(socket, "AF_UNIX")
    address = str(tmp_path / "b.sock") if use_unix else "tcp:127.0.0.1:0"
    server = await serve_registry_socket(
        ToolRegistry(), address, token=BRIDGE_TOKEN,
        http_token=HTTP_TOKEN, http_routes={USAGE_PATH: route},
    )
    if not use_unix:
        address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
    return server, address


async def _open(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if address.startswith("tcp:"):
        host, _, port = address[4:].rpartition(":")
        return await asyncio.open_connection(host, int(port))
    return await asyncio.open_unix_connection(address)


async def _post(
    address: str, body: bytes | str | dict, *, token: str | None = HTTP_TOKEN,
    path: str = USAGE_PATH, method: str = "POST", content_type: str = "application/json",
    length: int | None = None, extra: str = "",
) -> int:
    raw = body if isinstance(body, bytes) else (
        body.encode() if isinstance(body, str) else json.dumps(body).encode()
    )
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if token is not None:
        head += f"Authorization: Bearer {token}\r\n"
    if content_type:
        head += f"Content-Type: {content_type}\r\n"
    if length != -1:
        head += f"Content-Length: {len(raw) if length is None else length}\r\n"
    reader, writer = await _open(address)
    writer.write((head + extra + "\r\n").encode() + raw)
    await writer.drain()
    status_line = await asyncio.wait_for(reader.readline(), 10)
    writer.close()
    return int(status_line.split()[1])


@pytest.fixture
async def bridge(tmp_path):
    seen: list[Any] = []

    async def route(body: Any) -> None:
        if body == "refuse":
            raise ValueError("refused")
        if body == "explode":
            raise RuntimeError("host failure")
        seen.append(body)

    server, address = await _served(tmp_path, route)
    try:
        yield address, seen
    finally:
        server.close()
        await server.wait_closed()


async def test_an_authorized_post_reaches_the_route(bridge) -> None:
    address, seen = bridge
    assert await _post(address, _body()) == 204
    assert seen == [_body()]


@pytest.mark.parametrize("token", [None, "wrong", BRIDGE_TOKEN, ""])
async def test_a_post_without_the_route_token_is_refused(bridge, token) -> None:
    address, seen = bridge
    assert await _post(address, _body(), token=token) == 401
    assert seen == []


async def test_the_route_token_does_not_call_tools(bridge) -> None:
    address, _ = bridge
    reader, writer = await _open(address)
    writer.write((json.dumps({"op": "list", "token": HTTP_TOKEN}) + "\n").encode())
    await writer.drain()
    reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
    writer.close()
    assert "unauthorized" in reply["error"]


async def test_the_bridge_still_answers_its_own_protocol(bridge) -> None:
    address, _ = bridge
    reader, writer = await _open(address)
    writer.write((json.dumps({"op": "list", "token": BRIDGE_TOKEN}) + "\n").encode())
    await writer.drain()
    reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
    writer.close()
    assert reply == {"tools": []}


@pytest.mark.parametrize(("kwargs", "status"), [
    ({"path": "/elsewhere"}, 404),
    ({"method": "GET", "body": b""}, 405),
    ({"content_type": "text/plain"}, 415),
    ({"content_type": ""}, 415),
    ({"length": -1}, 411),
])
async def test_only_a_json_post_to_the_route_is_taken(bridge, kwargs, status) -> None:
    address, seen = bridge
    body = kwargs.pop("body", _body())
    assert await _post(address, body, **kwargs) == status
    assert seen == []


async def test_an_oversized_body_is_refused_unread(bridge) -> None:
    address, seen = bridge
    assert await _post(address, b"{}", length=HTTP_MAX_BODY_BYTES + 1) == 413
    assert seen == []


async def test_oversized_headers_are_refused(bridge) -> None:
    address, seen = bridge
    extra = "".join(f"X-Pad-{i}: {'a' * 200}\r\n" for i in range(50))
    assert await _post(address, _body(), extra=extra) == 431
    assert seen == []


@pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe", "refuse"])
async def test_a_bad_body_is_a_400(bridge, body) -> None:
    address, seen = bridge
    payload = json.dumps(body).encode() if isinstance(body, str) else body
    assert await _post(address, payload) == 400
    assert seen == []


async def test_a_host_failure_is_not_the_callers(bridge) -> None:
    address, _ = bridge
    assert await _post(address, json.dumps("explode").encode()) == 204


async def test_a_silent_client_does_not_hold_the_bridge(bridge, monkeypatch) -> None:
    monkeypatch.setattr(bundled_server, "_HTTP_READ_TIMEOUT", 0.2)
    address, seen = bridge
    reader, writer = await _open(address)
    writer.write(f"POST {USAGE_PATH} HTTP/1.1\r\n".encode())
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    assert seen == []


async def test_http_routes_need_their_own_token(tmp_path) -> None:
    calls: list[Any] = []

    async def route(body: Any) -> None:
        calls.append(body)

    use_unix = hasattr(socket, "AF_UNIX")
    address = str(tmp_path / "c.sock") if use_unix else "tcp:127.0.0.1:0"
    server = await serve_registry_socket(
        ToolRegistry(), address, token=BRIDGE_TOKEN, http_routes={USAGE_PATH: route},
    )
    if not use_unix:
        address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        reader, writer = await _open(address)
        writer.write(f"POST {USAGE_PATH} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        # Without an HTTP token the line is not HTTP to this bridge: it is a
        # JSON line that does not parse, and nothing answers it.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), 0.3)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert calls == []


class _Writer:
    def __init__(self, peer: Any) -> None:
        self.peer = peer

    def get_extra_info(self, name: str) -> Any:
        return self.peer if name == "peername" else None


@pytest.mark.parametrize(("peer", "local"), [
    (("127.0.0.1", 5000), True), (("::1", 5000, 0, 0), True), (("127.0.0.2", 1), True),
    (("10.0.0.200", 5000), False), (("192.168.1.5", 1), False), (("fe80::1%eth0", 1, 0, 2), False),
    (("not-an-ip", 1), False), ("", True), (None, True),
])
def test_only_local_peers_are_served(peer, local) -> None:
    assert bundled_server._peer_is_local(_Writer(peer)) is local  # type: ignore[arg-type]


# ─── The loop's wiring ───────────────────────────────────────────────────────


_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)


def _loop(tmp_path: Path, version: str, **kwargs: Any) -> tuple[DelegatedLoop, Path]:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"version": f"{version} (Claude Code)"}), encoding="utf-8")
    record = tmp_path / "record.jsonl"
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_RECORD": str(record)},
        registry=ToolRegistry(),
        **kwargs,
    )
    return loop, record


async def _turn(loop: DelegatedLoop) -> None:
    if loop._session_id is None:
        loop._session_id = (await loop.store.create_session()).session_id
    sid = loop._session_id
    start = loop.bus.last_seq(sid)

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, since_seq=start):
            if isinstance(event, _TERMINALS):
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await loop.submit(UserTurn(content="hi"))
    await asyncio.wait_for(watcher, 20)
    await asyncio.wait_for(loop.wait_turn(), 20)


def _spawn_env(record: Path) -> dict[str, str]:
    for line in record.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("kind") == "start":
            return entry["env"]
    raise AssertionError("the fake CLI recorded no start")


async def test_the_mod_gets_the_route_and_a_report_reaches_the_host(tmp_path) -> None:
    reports: list[PlanUsageReport] = []

    async def sink(report: PlanUsageReport) -> None:
        reports.append(report)

    loop, record = _loop(tmp_path, "2.1.287", claude_mod=True, plan_usage_sink=sink)
    try:
        await _turn(loop)
        env = _spawn_env(record)
        url, token = env[CLAUDE_MOD_URL_ENV], env[CLAUDE_MOD_TOKEN_ENV]
        assert url.endswith(USAGE_PATH)
        # Its own token: the bridge token never reaches the CLI's environment.
        assert token and token != loop._bridge_token
        assert loop._bridge_token not in json.dumps(env)
        if url.startswith("http://127.0.0.1:"):
            address = "tcp:" + url[len("http://"):-len(USAGE_PATH)]
            assert CLAUDE_MOD_SOCKET_ENV not in env
        else:
            address = env[CLAUDE_MOD_SOCKET_ENV]
        body = _body(measuredAt=time.time() * 1000)
        assert await _post(address, body, token=token) == 204
        assert [w.kind for w in reports[0].windows] == ["five_hour", "seven_day"]
        bad = _body(measuredAt=time.time() * 1000, rateLimits=[{"kind": "x", "percentUsed": -3}])
        assert await _post(address, bad, token=token) == 400
        assert len(reports) == 1
    finally:
        await loop.aclose()


@pytest.mark.parametrize(("version", "claude_mod", "with_sink"), [
    ("2.1.286", True, True),    # a CLI without mods
    ("2.1.287", False, True),   # the off switch
    ("2.1.287", True, False),   # no host takes reports
])
async def test_no_route_reaches_the_cli_without_mod_and_host(
    tmp_path, version, claude_mod, with_sink,
) -> None:
    async def sink(report: PlanUsageReport) -> None:
        raise AssertionError("no report expected")

    loop, record = _loop(
        tmp_path, version, claude_mod=claude_mod, plan_usage_sink=sink if with_sink else None,
    )
    try:
        await _turn(loop)
        env = _spawn_env(record)
        for name in (CLAUDE_MOD_URL_ENV, CLAUDE_MOD_TOKEN_ENV, CLAUDE_MOD_SOCKET_ENV):
            assert name not in env
    finally:
        await loop.aclose()


async def test_a_failing_host_never_fails_the_turn(tmp_path) -> None:
    async def sink(report: PlanUsageReport) -> None:
        raise RuntimeError("database down")

    loop, record = _loop(tmp_path, "2.1.287", claude_mod=True, plan_usage_sink=sink)
    try:
        await _turn(loop)
        env = _spawn_env(record)
        url = env[CLAUDE_MOD_URL_ENV]
        address = (
            "tcp:" + url[len("http://"):-len(USAGE_PATH)]
            if url.startswith("http://127.0.0.1:") else env[CLAUDE_MOD_SOCKET_ENV]
        )
        assert await _post(address, _body(measuredAt=time.time() * 1000),
                           token=env[CLAUDE_MOD_TOKEN_ENV]) == 204
        await _turn(loop)
    finally:
        await loop.aclose()
