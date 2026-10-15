"""Files confinement and read-only Monitor attachment through real owner auth."""

import asyncio
import shlex
from unittest.mock import AsyncMock

import pytest

from app.api import terminals as api
from app.mcp import builtin_tools as bt
from tests.api.test_terminals_api import (  # shared real-owner/PTY fixture
    ctx, owner, wsc, close_code, until, next_json, attach, read_until, strip,
)


def test_files_relative_root_and_subfolder_open_in_real_shell(ctx):
    tc = owner(ctx)
    for path in ("", "work"):
        response = tc.post("/api/v1/terminals/from-files", json={"path": path})
        assert response.status_code == 201, response.text
        terminal = response.json()["terminal"]
        expected = (ctx.tmp / path).resolve()
        assert terminal["cwd"] == str(expected)
        with wsc(tc, f"/api/v1/ws/terminals/{terminal['id']}") as ws:
            attach(ws)
            ws.send_json({"type": "input", "data": "pwd; echo FOLDER_$((6*7))\n"})
            assert str(expected).encode() in strip(read_until(ws, b"FOLDER_42"))


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink"])
def test_cwd_and_files_refuse_escape(ctx, kind):
    outside = ctx.tmp.parent
    if kind == "absolute":
        path = str(outside)
    elif kind == "traversal":
        path = "work/../.."
    else:
        (ctx.tmp / "escape").symlink_to(outside, target_is_directory=True)
        path = "escape"
    tc = owner(ctx)
    for route, body in (
        ("/from-files", {"path": path}),
        ("", {"cwd": str(ctx.tmp / path)}),
    ):
        response = tc.post("/api/v1/terminals" + route, json=body)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Path escapes workspace boundary"
    assert ctx.reg.count == 0


def test_files_missing_file_and_inside_symlink(ctx):
    tc = owner(ctx)
    (ctx.tmp / "file").write_text("not a folder")
    for path in ("file", "missing"):
        response = tc.post("/api/v1/terminals/from-files", json={"path": path})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_folder"
    (ctx.tmp / "link").symlink_to(ctx.work, target_is_directory=True)
    response = tc.post("/api/v1/terminals", json={"cwd": str(ctx.tmp / "link")})
    assert response.status_code == 201
    assert response.json()["terminal"]["cwd"] == str(ctx.work.resolve())


@pytest.fixture
def shells(ctx, monkeypatch):
    # Keep the real subprocess/stdout producer. Only durable session event
    # publication is irrelevant to this API test and replaced here.
    monkeypatch.setattr(bt, "_background_tasks", {})
    monkeypatch.setattr(bt, "_emit_shell_start_frame", AsyncMock())
    monkeypatch.setattr(bt, "_emit_shell_settle_frame", AsyncMock())
    tc = owner(ctx)
    tasks = []

    def spawn(command, session="watch-session"):
        task = tc.portal.call(bt._spawn_background, command, str(ctx.tmp), session)
        tasks.append(task)
        return task

    yield tc, spawn

    async def cleanup():
        for task in tasks:
            if task.is_running():
                task.process.kill()
            if task._reader_task:
                await asyncio.wait_for(task._reader_task, 10)
    tc.portal.call(cleanup)


def output_url(task, session="watch-session"):
    return f"/api/v1/terminals/monitor/{task.task_id}?session_id={session}"


def socket_url(task, session="watch-session"):
    return f"/api/v1/ws/terminals/monitor/{task.task_id}?session_id={session}"


def test_finished_monitor_backlog_and_session_scoping(shells):
    tc, spawn = shells
    task = spawn("printf 'first\nsecond\n'")
    assert until(lambda: task._reader_task.done())
    rows = tc.get("/api/v1/terminals/monitor?session_id=watch-session").json()["tasks"]
    assert [row["id"] for row in rows] == [task.task_id]
    assert rows[0]["subject"] == task.command
    assert rows[0]["status"] == "completed"
    assert tc.get("/api/v1/terminals/monitor?session_id=other").json() == {"tasks": []}
    snapshot = tc.get(output_url(task)).json()
    assert snapshot["lines"] == ["first", "second"]
    assert snapshot["readonly"] is True and snapshot["finished"] is True
    assert snapshot["task"]["exit_code"] == 0
    with wsc(tc, socket_url(task)) as ws:
        assert ws.receive_json() == snapshot
        assert close_code(ws) == 1000
    assert tc.get(output_url(task, "other")).status_code == 404
    with wsc(tc, socket_url(task, "other")) as ws:
        assert close_code(ws) == 4004
    assert tc.get("/api/v1/terminals/monitor/unknown?session_id=watch-session").status_code == 404
    with wsc(tc, "/api/v1/ws/terminals/monitor/unknown?session_id=watch-session") as ws:
        assert close_code(ws) == 4004


@pytest.mark.parametrize("line_limit", [5000, 2])
def test_monitor_live_tail_uses_real_stdout_and_drains_before_close(shells, ctx, monkeypatch, line_limit):
    monkeypatch.setattr(bt, "_BACKGROUND_LINE_LIMIT", line_limit)
    tc, spawn = shells
    release = ctx.tmp / "release"
    task = spawn(f"printf 'backlog\n'; while [ ! -f {shlex.quote(str(release))} ]; "
                 "do sleep 0.05; done; printf 'live\nlast-without-newline'")
    assert until(lambda: task.lines == ["backlog"])
    with wsc(tc, socket_url(task)) as ws:
        first = ws.receive_json()
        assert first["lines"] == ["backlog"] and first["finished"] is False
        release.touch()
        while True:
            snapshot = next_json(ws, "output")
            if snapshot["finished"]:
                break
        assert snapshot["lines"] == ["backlog", "live", "last-without-newline"][-line_limit:]
        assert close_code(ws) == 1000


@pytest.mark.parametrize("frame", [{"type": "input", "data": "exit\n"},
                                    {"type": "resize", "cols": 80, "rows": 24}])
def test_monitor_refuses_input_and_resize_without_stopping_task(shells, ctx, frame):
    tc, spawn = shells
    release = ctx.tmp / "release"
    task = spawn(f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.05; done")
    with wsc(tc, socket_url(task)) as ws:
        assert ws.receive_json()["readonly"] is True
        ws.send_json(frame)
        assert close_code(ws) == 1008
    assert task.is_running()
    release.touch()


def test_monitor_disconnect_and_owner_revocation_do_not_stop_shell(shells, ctx, monkeypatch):
    from app.core import owner_auth

    monkeypatch.setattr(api, "AUTH_RECHECK_SECONDS", 0.02)
    tc, spawn = shells
    release = ctx.tmp / "release"
    task = spawn(f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.05; done")
    with wsc(tc, socket_url(task)) as ws:
        ws.receive_json()
    assert task.is_running()
    with wsc(tc, socket_url(task)) as ws:
        ws.receive_json()
        owner_auth.revoke_local_session(ctx.owner)
        assert close_code(ws) == 4001
    assert task.is_running()
    release.touch()


def test_monitor_snapshot_has_an_additional_character_bound(shells, monkeypatch):
    tc, spawn = shells
    task = spawn("printf 'one\ntwo\nthree\n'")
    assert until(lambda: task._reader_task.done())
    monkeypatch.setattr(api, "MONITOR_SNAPSHOT_CHARS", 10)
    snapshot = tc.get(output_url(task)).json()
    assert snapshot["lines"] == ["two", "three"]
    assert snapshot["truncated"] is True
    assert sum(len(line) + 1 for line in snapshot["lines"]) <= 10
