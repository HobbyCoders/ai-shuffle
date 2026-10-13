"""In-app terminal service (#1945): real shells on real PTYs.

Covers the scrollback bounds, paste cleaning (never executes), the shell
environment (no app secrets, asserted by name only, values are never
printed), folder resolution, the cap, restart, busy detection, logging, and
that closing or stopping kills the whole process session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys

import pytest

from app.core import terminals as T

pytestmark = [
    pytest.mark.skipif(os.name == "nt" or not T.pty_supported(), reason="needs a Unix PTY"),
    pytest.mark.timeout(60),
]

WORKER = {"id": "dev_test", "name": "TestBox", "platform": "linux", "container": True}


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setattr(T, "worker_identity", lambda: dict(WORKER))


@pytest.fixture
async def reg():
    registry = T.TerminalRegistry()
    yield registry
    await registry.shutdown()


async def _until(predicate, timeout=8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _ready(terminal):
    assert await _until(lambda: terminal.state == T.STATE_LIVE)
    await terminal.wait_ready()


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
        return stat[stat.rfind(b")") + 2:].split()[0] != b"Z"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Scrollback
# ---------------------------------------------------------------------------

def test_scrollback_keeps_the_last_lines():
    sb = T.Scrollback(max_bytes=10_000, max_lines=5)
    for i in range(20):
        sb.append(f"line {i}\n".encode())
    assert sb.snapshot() == b"".join(f"line {i}\n".encode() for i in range(15, 20))
    assert sb.lines == 5


def test_scrollback_byte_cap_trims_at_a_line_boundary():
    sb = T.Scrollback(max_bytes=64, max_lines=5000)
    for i in range(40):
        sb.append(b"abcdefghi\n")
    snap = sb.snapshot()
    assert len(snap) <= 64 and snap.startswith(b"abcdefghi\n") and sb.size == len(snap)


def test_scrollback_is_bounded_for_a_single_huge_line():
    sb = T.Scrollback(max_bytes=1024, max_lines=10)
    for _ in range(100):
        sb.append(b"x" * 100)
    assert sb.size <= 1024


def test_default_limits_match_the_contract():
    assert T.SCROLLBACK_MAX_LINES == 5000 and T.SCROLLBACK_MAX_BYTES == 2 * 1024 * 1024
    assert T.TERMINAL_LIMIT == 8


# ---------------------------------------------------------------------------
# Paste cleaning
# ---------------------------------------------------------------------------

def test_clean_paste_strips_trailing_newlines_and_controls():
    assert T.clean_paste("ls -la\r\n\r\n") == "ls -la"
    assert T.clean_paste("a\rb\r\nc\n") == "a\nb\nc"
    assert T.clean_paste("x\x03y\x04z\x1b[201~w\x9b") == "xyz[201~w"
    assert T.clean_paste("tab\there") == "tab\there"


def test_paste_bytes_bracketed_cannot_be_broken_out_of():
    data, mode = T.paste_bytes("echo hi\x1b[201~\nrm -rf /tmp/x\n", bracketed=True)
    assert mode == "bracketed"
    assert data.startswith(b"\x1b[200~") and data.endswith(b"\x1b[201~")
    assert data.count(b"\x1b") == 2  # only our own markers
    assert not data.endswith(b"\n\x1b[201~")


def test_paste_bytes_plain_mode_refuses_multiline():
    data, mode = T.paste_bytes("echo one\t two\n", bracketed=False)
    assert (data, mode) == (b"echo one  two", "plain")
    with pytest.raises(T.TerminalError) as err:
        T.paste_bytes("one\ntwo", bracketed=False)
    assert err.value.code == "paste_multiline_unsupported"
    assert "use bash" in err.value.message
    with pytest.raises(T.TerminalError) as err:
        T.paste_bytes("one\ntwo", bracketed=False, shell="bash")
    assert err.value.code == "paste_multiline_unsupported" and "use bash" not in err.value.message


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

SECRET_NAMES = [
    "DATABASE_URL", "PGPASSWORD", "POSTGRES_PASSWORD", "SECRET_KEY", "ENCRYPTION_KEY",
    "SESSION_SIGNING_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "FAL_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "OWNER_PASSWORD",
    "CLUSTER_SECRET", "SHUFFLE_DESKTOP_LISTENER_TOKEN", "LICENSE_KEY", "SENTRY_DSN",
    "SSH_AUTH_SOCK", "FLUID_CHILD_ENV_PASSTHROUGH",
]


def test_shell_environment_drops_every_secret_by_name():
    source = {name: "placeholder" for name in SECRET_NAMES}
    source.update({"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8",
                   "HTTPS_PROXY": "http://user:pw@proxy:3128", "NO_PROXY": "localhost",
                   "FLUID_CHILD_ENV_PASSTHROUGH": "FAL_KEY,GH_TOKEN"})
    env = T.shell_environment("bash", "/bin/bash", source)
    leaked = sorted(set(SECRET_NAMES + ["HTTPS_PROXY"]) & set(env))
    assert leaked == []  # names only: values are never printed
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/home/u" and env["NO_PROXY"] == "localhost"
    assert env["TERM"] == "xterm-256color" and env["SHELL"] == "/bin/bash"



def test_shell_environment_drops_urls_with_a_credential_in_the_query():
    source = {"PATH": "/usr/bin", "HOME": "/home/u",
              "SERVICE_URL": "https://hub.example/api?x=1&access_token=placeholder",
              "UPSTREAM": "https://cdn.example/f?sig=placeholder&expires=1",
              "PLAIN_URL": "https://example.com/docs?page=2"}
    env = T.shell_environment("bash", "/bin/bash", source)
    assert "SERVICE_URL" not in env and "UPSTREAM" not in env
    assert env["PLAIN_URL"] == "https://example.com/docs?page=2"


def test_shell_keeps_the_server_run_marker_so_the_reaper_owns_orphans():
    """Not a credential: it is how the #1913 reaper finds a terminal shell a
    crashed server left behind (and never touches a live server's)."""
    from app.core import process_ledger

    env = T.shell_environment("bash", "/bin/bash",
                              {"PATH": "/usr/bin", process_ledger.RUN_MARKER: "run-1"})
    assert env[process_ledger.RUN_MARKER] == "run-1"

async def test_running_shell_does_not_see_a_secret(reg, monkeypatch, tmp_path):
    monkeypatch.setenv("TERMTEST_WEBHOOK_SECRET", "placeholder")
    monkeypatch.setenv("TERMTEST_PLAIN", "visible")
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    viewer, _ = term.attach()
    await _ready(term)
    await term.write(b'test -z "${TERMTEST_WEBHOOK_SECRET+set}" && echo ABSENT_$((40+2)); '
                     b'test -n "$TERMTEST_PLAIN" && echo PLAIN_$((40+3))\n')
    out = bytearray()

    def seen():
        for kind, data in viewer.drain():
            if kind == "output":
                out.extend(data)
        return b"ABSENT_42" in out and b"PLAIN_43" in out

    assert await _until(seen)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

def test_requested_folder_must_exist_and_be_a_directory(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    for bad in ("relative/path", str(tmp_path / "missing"), str(tmp_path / "file.txt"), "  "):
        with pytest.raises(T.TerminalError) as err:
            T.resolve_folder(None, bad)
        assert err.value.code == "bad_folder" and err.value.status == 400
    assert T.resolve_folder(None, str(tmp_path)) == (str(tmp_path), "requested")


def test_requested_folder_wins_over_the_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "chat_folder", lambda sid: pytest.fail("chat lookup not expected"))
    assert T.resolve_folder("sess-1", str(tmp_path)) == (str(tmp_path), "requested")


def test_chat_worktree_then_project_then_workspace(tmp_path, monkeypatch):
    from app.core import checkout_manager, project_resolver
    from app.db import database

    base, worktree = tmp_path / "base", tmp_path / "wt"
    base.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(database, "get_session", lambda sid: {"id": sid, "project_id": None})
    monkeypatch.setattr(project_resolver, "resolve_project_v2", lambda row: {"id": "p1"})
    monkeypatch.setattr(project_resolver, "_local_checkout", lambda *a, **k: {"path": str(base)})
    monkeypatch.setattr(checkout_manager, "_resolve_checkout_path", lambda p: p)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(worktree))
    assert T.resolve_folder("s1", None) == (str(worktree), "worktree")
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(base))
    assert T.resolve_folder("s1", None) == (str(base), "project")
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: None)
    assert T.resolve_folder("s1", None) == (str(base), "project")
    monkeypatch.setattr(project_resolver, "resolve_project_v2", lambda row: None)
    monkeypatch.setattr(T, "default_folder", lambda: (str(tmp_path), "workspace"))
    assert T.resolve_folder("s1", None) == (str(tmp_path), "workspace")


def test_unknown_chat_is_refused(monkeypatch):
    from app.db import database

    monkeypatch.setattr(database, "get_session", lambda sid: None)
    with pytest.raises(T.TerminalError) as err:
        T.resolve_folder("nope", None)
    assert err.value.code == "chat_not_found" and err.value.status == 404


def test_chat_on_another_worker_is_refused(monkeypatch):
    from app.db import database
    from app.network import control

    monkeypatch.setattr(database, "get_session", lambda sid: {"id": sid, "owner_device_id": "dev_other"})
    monkeypatch.setattr(control, "owner_of", lambda row: "dev_other")
    with pytest.raises(T.TerminalError) as err:
        T.resolve_folder("remote-chat", None)
    assert err.value.code == "chat_on_other_worker" and err.value.status == 409


# ---------------------------------------------------------------------------
# Shells and support
# ---------------------------------------------------------------------------

def test_only_existing_shells_are_offered(monkeypatch):
    shells = T.available_shells()
    assert shells and shells[0]["default"] and all(os.path.isfile(s["path"]) for s in shells)
    assert [s["name"] for s in shells] == [n for n in ("bash", "sh") if n in {s["name"] for s in shells}]
    monkeypatch.setattr(T, "_SHELLS", (("nosuchshell", ("/nonexistent/nosuchshell",)), ("sh", ("/bin/sh",))))
    assert [s["name"] for s in T.available_shells()] == ["sh"]


async def test_unsupported_worker_refuses(reg, monkeypatch):
    monkeypatch.setattr(T, "pty_supported", lambda: False)
    assert T.capability_version() == 0
    with pytest.raises(T.TerminalError) as err:
        await reg.create(created_by="owner")
    assert err.value.code == "not_supported"
    assert err.value.message == "Terminals are not supported on this worker yet."


async def test_unknown_shell_is_refused(reg, tmp_path):
    with pytest.raises(T.TerminalError) as err:
        await reg.create(created_by="owner", cwd=str(tmp_path), shell="zsh-not-here")
    assert err.value.code == "bad_shell"


# ---------------------------------------------------------------------------
# Lifecycle against real shells
# ---------------------------------------------------------------------------

async def test_create_list_and_attach(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash", cols=100, rows=30)
    assert term.id.startswith("term_") and term.target == {"kind": "worker", "worker_id": "dev_test"}
    await _ready(term)
    info = term.to_dict()
    assert info["state"] == "live" and info["cwd"] == str(tmp_path) and info["folder_source"] == "requested"
    assert (info["cols"], info["rows"]) == (100, 30) and info["busy"] is False
    assert [t.id for t in reg.list()] == [term.id] and reg.list("other-chat") == []
    viewer, replay = term.attach()
    assert replay  # the prompt
    await term.write(b"echo HELLO_$((6*7))\n")
    out = bytearray()

    def seen():
        out.extend(b"".join(d for k, d in viewer.drain() if k == "output"))
        return b"HELLO_42" in out

    assert await _until(seen)
    assert b"HELLO_42" in term.attach()[1]  # a second attachment replays it


async def test_bracketed_paste_never_executes(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    assert await _until(lambda: term.bracketed)
    marker = tmp_path / "pasted-ran"
    mode = await reg.paste(term.id, f"touch {marker}\ntouch {marker}2\r\n\n")
    assert mode == "bracketed"
    await asyncio.sleep(1.0)
    assert not marker.exists() and not (tmp_path / "pasted-ran2").exists()
    # The text sits at the prompt (readline draws it highlighted).
    shown = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", term.scrollback.snapshot())
    assert b"pasted-ran2" in shown


async def test_plain_paste_in_sh_never_executes(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    marker = tmp_path / "plain-ran"
    assert await reg.paste(term.id, f"touch {marker}\n") == "plain"
    await asyncio.sleep(0.8)
    assert not marker.exists()
    with pytest.raises(T.TerminalError) as err:
        await reg.paste(term.id, "echo a\necho b")
    assert err.value.code == "paste_multiline_unsupported"


# A paste must never run before the owner presses Enter, whatever the shell
# printed before. ``ESC[?2004h`` in output (a cat, a printf, a program killed
# with the mode on) is not proof the shell holds a bracketed paste.

STALE_ON = r"printf '\033[?2004h'"


async def _run(term, command: str, n: int) -> None:
    """Type ``command`` + Enter and wait until it has finished. The token is
    computed by the shell so the echoed command line cannot match it."""
    await term.write(f"{command}; echo DONE_$((1000+{n}))\n".encode())
    assert await _until(lambda: f"DONE_{1000 + n}".encode() in term.scrollback.snapshot())
    await asyncio.sleep(0.3)
    await term.wait_ready()


def _ran(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("ran"))


async def _paste_three_lines(reg, term, tmp_path):
    lines = "\n".join(f"touch {tmp_path / f'ran{i}'}" for i in (1, 2, 3))
    try:
        return await reg.paste(term.id, lines + "\n")
    finally:
        await asyncio.sleep(1.0)


async def test_sh_with_a_stale_bracketed_on_refuses_a_multiline_paste(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    await _run(term, STALE_ON, 1)
    assert term.bracketed  # the output switched the mode "on"; sh cannot honour it
    with pytest.raises(T.TerminalError) as err:
        await _paste_three_lines(reg, term, tmp_path)
    assert err.value.code == "paste_multiline_unsupported" and err.value.status == 422
    assert "Paste one line" in err.value.message
    assert _ran(tmp_path) == []


async def test_sh_with_a_stale_bracketed_on_pastes_one_line_plain(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    await _run(term, STALE_ON, 2)
    before = len(term.scrollback.snapshot())
    assert await reg.paste(term.id, f"touch {tmp_path / 'ran1'}\n") == "plain"
    await asyncio.sleep(0.8)
    assert _ran(tmp_path) == []
    # Written bare: no bracketed-paste markers reach sh's line.
    assert b"200~" not in term.scrollback.snapshot()[before:]
    await term.write(b"\r")
    assert await _until(lambda: _ran(tmp_path) == ["ran1"])


async def test_bash_holds_a_multiline_paste_until_enter(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    assert await _until(lambda: term.bracketed)
    assert await _paste_three_lines(reg, term, tmp_path) == "bracketed"
    assert _ran(tmp_path) == []
    await term.write(b"\r")
    assert await _until(lambda: _ran(tmp_path) == ["ran1", "ran2", "ran3"])


async def test_bash_single_line_paste_does_not_run(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    assert await reg.paste(term.id, f"touch {tmp_path / 'ran1'}") == "bracketed"
    await asyncio.sleep(0.8)
    assert _ran(tmp_path) == []


async def test_bash_with_bracketed_paste_off_refuses_a_multiline_paste(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    await _run(term, "bind 'set enable-bracketed-paste off'", 3)
    assert not term.bracketed  # readline's last word was ESC[?2004l
    with pytest.raises(T.TerminalError) as err:
        await _paste_three_lines(reg, term, tmp_path)
    assert err.value.code == "paste_multiline_unsupported"
    assert "Paste one line" in err.value.message and "use bash" not in err.value.message
    assert _ran(tmp_path) == []
    assert await reg.paste(term.id, f"touch {tmp_path / 'ran1'}") == "plain"
    await asyncio.sleep(0.8)
    assert _ran(tmp_path) == []


@pytest.mark.parametrize("setup", ["set +o emacs", "exec sh"])
async def test_bash_without_readline_ignores_a_stale_bracketed_on(reg, tmp_path, setup):
    # No line editor (or no bash any more) reads the line, so the PTY stays
    # in canonical mode; output claiming the mode is on must not count.
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    if setup == "exec sh":
        await term.write(b"exec sh\n")
        await asyncio.sleep(0.5)
        await term.wait_ready()
    else:
        await _run(term, setup, 4)
    await _run(term, STALE_ON, 5)
    assert term.bracketed
    with pytest.raises(T.TerminalError) as err:
        await _paste_three_lines(reg, term, tmp_path)
    assert err.value.code == "paste_multiline_unsupported"
    assert _ran(tmp_path) == []


async def test_busy_terminal_refuses_paste(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    await _ready(term)
    await term.write(b"sleep 20\n")
    assert await _until(lambda: term.refresh_busy() or term.busy)
    with pytest.raises(T.TerminalError) as err:
        await reg.paste(term.id, "echo nope")
    assert err.value.code == "terminal_busy"
    # Ctrl-C sent between bash's fork and sleep's exec is swallowed by the
    # child (it still has bash's SIGINT handling), so keep sending it until
    # the shell has the terminal back, as a person would.
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        await term.write(b"\x03")
        if await _until(lambda: (term.refresh_busy(), not term.busy)[1], timeout=0.5):
            break
    assert (term.refresh_busy(), not term.busy)[1]


async def test_close_kills_the_whole_process_session(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
    viewer, _ = term.attach()
    await _ready(term)
    await term.write(b"sleep 300 &\nsleep 301 | cat\n")
    sid = term.pid
    assert await _until(lambda: len(T._session_members(sid)) >= 4)
    members = T._session_members(sid)
    await reg.close(term.id, by="owner")
    assert await _until(lambda: not any(_alive(p) for p in members))
    assert reg.list() == [] and viewer.closed_reason == "closed"
    with pytest.raises(T.TerminalError) as err:
        reg.get(term.id)
    assert err.value.code == "not_found"


async def test_exit_and_restart_keep_id_and_scrollback(reg, tmp_path):
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    await term.write(b"echo BEFORE_$((1+1)); exit 3\n")
    assert await _until(lambda: term.state == T.STATE_EXITED)
    assert term.exit_code == 3 and term.exited_at
    with pytest.raises(T.TerminalError) as err:
        await reg.paste(term.id, "x")
    assert err.value.code == "terminal_exited"
    same = await reg.restart(term.id, by="owner")
    assert same is term and term.state in (T.STATE_STARTING, T.STATE_LIVE) and term.exit_code is None
    await _ready(term)
    assert b"BEFORE_2" in term.scrollback.snapshot()
    with pytest.raises(T.TerminalError) as err:
        await reg.restart(term.id, by="owner")
    assert err.value.code == "not_exited"


async def test_cap_counts_every_terminal_and_names_the_worker(tmp_path):
    reg = T.TerminalRegistry(limit=2)
    try:
        await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
        await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
        with pytest.raises(T.TerminalError) as err:
            await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
        assert err.value.code == "limit_reached" and err.value.status == 409
        assert err.value.message == "2 terminals are open on TestBox. Close one to open another."
    finally:
        await reg.shutdown()


async def test_bad_folder_does_not_open_a_terminal(reg, tmp_path):
    with pytest.raises(T.TerminalError) as err:
        await reg.create(created_by="owner", cwd=str(tmp_path / "gone"))
    assert err.value.code == "bad_folder" and reg.count == 0


async def test_shutdown_kills_everything_and_empties_the_list(tmp_path):
    reg = T.TerminalRegistry()
    terms = [await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh") for _ in range(2)]
    for term in terms:
        await _ready(term)
        await term.write(b"sleep 300 &\n")
    pids = [term.pid for term in terms]
    assert await _until(lambda: all(len(T._session_members(p)) >= 2 for p in pids))
    members = [m for p in pids for m in T._session_members(p)]
    await reg.shutdown()
    assert reg.list() == []
    assert await _until(lambda: not any(_alive(p) for p in members))


async def test_logs_open_close_and_exit_but_never_io(reg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="app.core.terminals")
    term = await reg.create(created_by="paired:pc1", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    await term.write(b"echo TYPED_SECRET_WORD\n")
    await reg.paste(term.id, "PASTED_SECRET_WORD")
    await asyncio.sleep(0.3)
    await term.write(b"\x15exit 0\n")
    assert await _until(lambda: term.state == T.STATE_EXITED)
    await reg.restart(term.id, by="owner")
    await reg.close(term.id, by="owner")
    text = "\n".join(r.getMessage() for r in caplog.records)
    opened = [r for r in caplog.records if r.getMessage().startswith("Terminal opened")]
    assert opened and opened[0].levelno == logging.INFO
    msg = opened[0].getMessage()
    assert term.id in msg and str(tmp_path) in msg and "shell=sh" in msg and "by=paired:pc1" in msg
    assert any(r.getMessage().startswith("Terminal exited") and r.levelno == logging.INFO for r in caplog.records)
    assert any(r.getMessage().startswith("Terminal closed") and "by=owner" in r.getMessage() for r in caplog.records)
    assert "SECRET_WORD" not in text


def test_viewer_outbox_is_bounded():
    viewer = T.Viewer(max_pending=100)
    viewer.push_output(b"x" * 60)
    viewer.push_output(b"y" * 60)
    assert viewer.overflowed and viewer.pending == 0 and not viewer.items


def test_never_signals_our_own_session(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(pid))
    T._signal_session(os.getsid(0), 9)
    T._signal_session(1, 9)
    T._signal_session(os.getpid(), 9)
    assert sent == []


async def test_spawn_uses_no_python_fork(reg, monkeypatch, tmp_path):
    """The shell is started with subprocess (fork+exec in C), never pty.fork,
    and it is a session leader with the PTY as its controlling terminal."""
    import pty

    monkeypatch.setattr(pty, "fork", lambda: pytest.fail("pty.fork used"))
    monkeypatch.setattr(os, "fork", lambda: pytest.fail("os.fork used"))
    term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="sh")
    await _ready(term)
    assert os.getsid(term.pid) == term.pid != os.getsid(0)
    assert os.tcgetpgrp(term._fd) == term.pid
    assert sys.executable


async def test_ctrl_c_works_when_the_app_was_started_with_sigint_ignored(reg, tmp_path):
    # A worker launched as a background job of a non-interactive shell
    # (`python -m app &` in an entrypoint script) inherits SIGINT/SIGQUIT as
    # ignored, and an interactive shell cannot un-ignore them for its
    # children. The spawn trampoline must reset them, or Ctrl-C never
    # interrupts a command. Regression for #2342.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        term = await reg.create(created_by="owner", cwd=str(tmp_path), shell="bash")
        await _ready(term)
        await term.write(b"sleep 20\n")
        assert await _until(lambda: term.refresh_busy() or term.busy)
        deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < deadline:
            await term.write(b"\x03")
            if await _until(lambda: (term.refresh_busy(), not term.busy)[1], timeout=0.5):
                break
        assert (term.refresh_busy(), not term.busy)[1]
    finally:
        signal.signal(signal.SIGINT, previous)
