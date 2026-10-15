"""Deterministic substitutions at the validation/spawn boundary, with real PTYs."""

import os

import pytest

from app.api import terminals as api
from app.core import terminals as service
from tests.api.test_terminals_api import (
    ctx, owner, wsc, attach, read_until, strip, until,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX rename and descriptor handoff; Windows pins are tested natively")


def folders(ctx, tmp_path_factory, component):
    parent = ctx.tmp / "parent"
    selected = parent / "selected"
    selected.mkdir(parents=True)
    outside = tmp_path_factory.mktemp("outside-files-root")
    (outside / "selected").mkdir()
    victim = parent if component == "ancestor" else selected
    moved = victim.with_name(victim.name + "-original")
    expected = moved / "selected" if component == "ancestor" else moved

    def swap():
        victim.rename(moved)
        victim.symlink_to(outside, target_is_directory=True)

    return selected, swap, expected, outside


def prepare(tc, selected, operation):
    if operation != "restart":
        return None
    response = tc.post("/api/v1/terminals", json={"cwd": str(selected)})
    assert response.status_code == 201, response.text
    tid = response.json()["terminal"]["id"]
    with wsc(tc, f"/api/v1/ws/terminals/{tid}") as ws:
        attach(ws)
        ws.send_json({"type": "input", "data": "exit\n"})
    assert until(lambda: tc.get(f"/api/v1/terminals/{tid}").json()["terminal"]["state"] == "exited")
    return tid


def open_or_restart(tc, selected, operation, tid):
    if operation == "restart":
        return tc.post(f"/api/v1/terminals/{tid}/restart")
    if operation == "files":
        return tc.post("/api/v1/terminals/from-files", json={"path": "parent/selected"})
    return tc.post("/api/v1/terminals", json={"cwd": str(selected)})


@pytest.mark.parametrize("component", ["directory", "ancestor"])
@pytest.mark.parametrize("operation", ["create", "files", "restart"])
def test_swap_after_path_validation_is_refused(ctx, tmp_path_factory, monkeypatch, component, operation):
    selected, swap, _expected, _outside = folders(ctx, tmp_path_factory, component)
    tc = owner(ctx)
    tid = prepare(tc, selected, operation)
    if operation == "restart":
        original = service.Terminal.spawn

        def spawn(terminal):
            swap()  # after restart's isdir check, before its no-follow open
            return original(terminal)
        monkeypatch.setattr(service.Terminal, "spawn", spawn)
    else:
        original = api._validated_terminal_cwd

        def validate(cwd):
            result = original(cwd)
            swap()  # the exact check-then-use gap from the security report
            return result
        monkeypatch.setattr(api, "_validated_terminal_cwd", validate)
    response = open_or_restart(tc, selected, operation, tid)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_folder"
    assert ctx.reg.count == (1 if tid else 0)


@pytest.mark.parametrize("component", ["directory", "ancestor"])
@pytest.mark.parametrize("operation", ["create", "files", "restart"])
def test_swap_after_directory_pin_never_redirects_child(ctx, tmp_path_factory, monkeypatch, component, operation):
    selected, swap, expected, outside = folders(ctx, tmp_path_factory, component)
    tc = owner(ctx)
    tid = prepare(tc, selected, operation)
    original = service.subprocess.Popen

    def popen(*args, **kwargs):
        assert kwargs["cwd"] == "/"  # no pathname re-resolution in Popen
        assert "preexec_fn" not in kwargs
        assert len(kwargs["pass_fds"]) == 2
        for fd in kwargs["pass_fds"]:
            os.fstat(fd)  # held through the real process creation
        swap()  # immediately before the real process creation
        return original(*args, **kwargs)
    monkeypatch.setattr(service.subprocess, "Popen", popen)
    response = open_or_restart(tc, selected, operation, tid)
    assert response.status_code in (200, 201), response.text
    terminal = response.json()["terminal"]
    with wsc(tc, f"/api/v1/ws/terminals/{terminal['id']}") as ws:
        attach(ws)
        ws.send_json({"type": "input", "data": "pwd -P; echo PIN_$((6*7))\n"})
        output = strip(read_until(ws, b"PIN_42"))
        assert str(expected).encode() in output
        assert str(outside).encode() not in output


def test_child_refuses_directory_moved_out_of_root_after_pin(ctx, tmp_path_factory, monkeypatch):
    selected, _swap, _expected, outside = folders(ctx, tmp_path_factory, "directory")
    tc = owner(ctx)
    original = service.subprocess.Popen

    def popen(*args, **kwargs):
        selected.rename(outside / "stolen")
        return original(*args, **kwargs)
    monkeypatch.setattr(service.subprocess, "Popen", popen)
    response = tc.post("/api/v1/terminals", json={"cwd": str(selected)})
    assert response.status_code == 201, response.text
    tid = response.json()["terminal"]["id"]
    assert until(lambda: ctx.reg.get(tid).state == "exited")
    terminal = ctx.reg.get(tid)
    assert terminal.exit_code != 0
    assert b"Terminal folder moved outside the Files root" in terminal.scrollback.snapshot()


@pytest.mark.parametrize("fail", [False, True])
def test_directory_pins_close_on_success_and_spawn_failure(tmp_path, monkeypatch, fail):
    selected = tmp_path / "selected"
    selected.mkdir()
    opened = []
    original = os.open

    def tracked(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(service.os, "open", tracked)
    try:
        with service._pin_folder(str(selected), str(tmp_path)) as (_path, fds):
            assert len(fds) == 2
            if fail:
                raise RuntimeError("spawn failed")
    except RuntimeError:
        assert fail
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
