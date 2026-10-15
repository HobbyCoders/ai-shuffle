"""Real ConPTY shells on Windows; no live database or app credentials."""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from app.core import terminals as T

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="needs Windows ConPTY"),
    pytest.mark.timeout(60),
]


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setattr(T, "worker_identity", lambda: {
        "id": "dev_test", "name": "WindowsTest", "platform": "windows", "container": False})


@pytest.fixture
async def reg():
    registry = T.TerminalRegistry()
    yield registry
    await registry.shutdown()


async def until(terminal, predicate, timeout=12):
    """Emulate xterm's cursor report so PSReadLine can start without a browser."""
    deadline = asyncio.get_running_loop().time() + timeout
    answered = getattr(terminal, "_test_dsr_answered", 0)
    while asyncio.get_running_loop().time() < deadline:
        output = terminal.scrollback.snapshot()
        queries = output.count(b"\x1b[6n")
        if queries > answered:
            await terminal.write(b"\x1b[1;1R" * (queries - answered))
            answered = queries
            terminal._test_dsr_answered = answered
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate(), "terminal condition timed out"


async def ready(terminal):
    await until(terminal, lambda: b"PS " in terminal.scrollback.snapshot())
    await terminal.wait_ready()


async def command(terminal, command, marker):
    await terminal.write(command.encode("utf-8") + b"\r")
    await until(terminal, lambda: marker in terminal.scrollback.snapshot())


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
async def test_echo_resize_scrollback_restart_close(reg, tmp_path, shell):
    if shell not in {s["name"] for s in T.available_shells()}:
        pytest.skip(f"{shell} is not installed")
    terminal = await reg.create(created_by="owner", cwd=str(tmp_path), shell=shell, cols=80, rows=24)
    try:
        assert isinstance(terminal, T.WindowsTerminal)
        assert terminal.shell in {"pwsh", "powershell"}
        assert reg.list() == [terminal]
        await ready(terminal)
        # The expected output is not present in the echoed command.
        await command(terminal, "echo ('he'+'llo')", b"hello")
        viewer, replay = terminal.attach()
        assert b"hello" in replay
        terminal.detach(viewer)
        terminal.resize(101, 37)
        await command(terminal, "echo ('SIZE_'+$Host.UI.RawUI.WindowSize.Width+'x'+$Host.UI.RawUI.WindowSize.Height)",
                      b"SIZE_101x37")
        assert (terminal.to_dict()["cols"], terminal.to_dict()["rows"]) == (101, 37)
        old_pid = terminal.pid
        await terminal.write(b"exit 7\r")
        assert await terminal.wait_exited(12)
        assert terminal.exit_code == 7
        assert reg.count == 1  # exited terminals still count toward the cap
        same = await reg.restart(terminal.id, by="owner")
        assert same is terminal and terminal.pid != old_pid
        assert b"hello" in terminal.scrollback.snapshot()
        # A new shell must really execute, not merely replay old output.
        await command(terminal, "echo ('re'+'started')", b"restarted")
    finally:
        await reg.close(terminal.id, by="owner")
    assert reg.count == 0
    assert terminal.state == T.STATE_EXITED
    assert terminal._thread is not None and not terminal._thread.is_alive()
    assert terminal._proc._process is None and terminal._proc._job is None


async def test_cap_shutdown_and_descendant_cleanup(reg, tmp_path):
    import psutil

    terminals = [await reg.create(created_by="owner", cwd=str(tmp_path)) for _ in range(8)]
    with pytest.raises(T.TerminalError) as error:
        await reg.create(created_by="owner", cwd=str(tmp_path))
    assert error.value.code == "limit_reached"
    terminal = terminals[0]
    await ready(terminal)
    await command(terminal, "$p=Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep 60' -PassThru; echo ('CHILD_'+$p.Id)", b"CHILD_")
    await until(terminal, lambda: re.search(rb"CHILD_(\d+)", terminal.scrollback.snapshot()) is not None)
    child = int(re.search(rb"CHILD_(\d+)", terminal.scrollback.snapshot()).group(1))
    assert psutil.pid_exists(child)
    assert terminal.refresh_busy() or terminal.busy
    pids = [t.pid for t in terminals]
    await reg.shutdown()
    assert reg.count == 0
    assert not psutil.pid_exists(child)
    assert all(not psutil.pid_exists(pid) for pid in pids)
    assert all(not t._thread.is_alive() for t in terminals)


async def test_environment_unicode_and_safe_paste(reg, tmp_path, monkeypatch):
    monkeypatch.setenv("SHUFFLE_TEST_SECRET", "sentinel-not-a-real-secret")
    folder = tmp_path / "folder with spaces é"
    folder.mkdir()
    terminal = await reg.create(created_by="owner", cwd=str(folder))
    await ready(terminal)
    await command(terminal, "echo ('ENV_'+[string]::IsNullOrEmpty($env:SHUFFLE_TEST_SECRET))", b"ENV_True")
    await command(terminal, "echo ([char]0x00e9+[string][char]0x03bb)", "éλ".encode())
    assert terminal.cwd == str(folder)
    assert not terminal.holds_bracketed_paste()
    with pytest.raises(T.TerminalError) as error:
        await reg.paste(terminal.id, "echo one\necho two")
    assert error.value.code == "paste_multiline_unsupported"
    before = terminal.scrollback.snapshot()
    assert await reg.paste(terminal.id, "echo ('PASTE_'+'HELD')\n") == "plain"
    await terminal.write(b"\r")
    await until(terminal, lambda: b"PASTE_HELD" in terminal.scrollback.snapshot()[len(before):])


def test_shell_preference_and_fallback(monkeypatch):
    assert T.pty_supported() and T.supported() and T.capability_version() == 2
    shells = T.available_shells()
    assert shells[0]["default"]
    assert all(os.path.isabs(shell["path"]) for shell in shells)
    monkeypatch.setattr(T.os.path, "isfile", lambda p: p.lower().endswith("powershell.exe"))
    monkeypatch.setattr(T.os, "access", lambda *args: True)
    monkeypatch.setattr(T.shutil, "which", lambda name: None)
    assert [s["name"] for s in T.available_shells()] == ["powershell"]


async def test_failed_spawn_leaves_no_registry_entry(reg, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "available_shells", lambda: [{"name": "powershell", "path": str(tmp_path / "missing.exe")}])
    with pytest.raises(T.TerminalError) as error:
        await reg.create(created_by="owner", cwd=str(tmp_path))
    assert error.value.code == "start_failed"
    assert reg.count == 0


async def test_input_after_exit_uses_existing_error_code(reg, tmp_path):
    terminal = await reg.create(created_by="owner", cwd=str(tmp_path))
    await ready(terminal)
    await terminal.write(b"exit 0\r")
    assert await terminal.wait_exited(12)
    with pytest.raises(T.TerminalError) as error:
        await terminal.write(b"echo ignored\r")
    assert error.value.code == "terminal_exited"


async def test_input_timeout_closes_writer_and_keeps_error_contract(reg, tmp_path, monkeypatch):
    terminal = await reg.create(created_by="owner", cwd=str(tmp_path))
    await ready(terminal)
    original = terminal._proc.write

    def stalled(data):
        # Models a full input pipe, unblocked by killing its console.
        terminal._proc.closed.wait(5)
        return original(data)

    monkeypatch.setattr(terminal._proc, "write", stalled)
    monkeypatch.setattr(T, "WRITE_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(T.TerminalError) as error:
        await terminal.write(b"echo ignored\r")
    assert error.value.code == "terminal_unresponsive"
    assert terminal.state == T.STATE_EXITED
    assert not terminal._thread.is_alive()


@pytest.mark.parametrize("component", ["directory", "ancestor"])
async def test_files_folder_and_ancestors_are_pinned_through_create_and_restart(reg, tmp_path, monkeypatch, component):
    parent = tmp_path / "parent"
    selected = parent / "selected"
    selected.mkdir(parents=True)
    victim = parent if component == "ancestor" else selected
    moved = victim.with_name(victim.name + "-moved")
    original = T.windows_conpty.ConPTYProcess
    attempts = []

    def create(*args, **kwargs):
        # Without the held no-delete-sharing handles an attacker can rename
        # this directory, then install a junction before CreateProcessW.
        with pytest.raises(OSError):
            victim.rename(moved)
        attempts.append(component)
        return original(*args, **kwargs)

    monkeypatch.setattr(T.windows_conpty, "ConPTYProcess", create)
    terminal = await reg.create(created_by="owner", cwd=str(selected), cwd_root=str(tmp_path))
    await ready(terminal)
    await terminal.write(b"exit\r")
    assert await terminal.wait_exited(12)
    await reg.restart(terminal.id, by="owner")
    await ready(terminal)
    assert attempts == [component, component]
