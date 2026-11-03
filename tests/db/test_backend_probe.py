"""The probe's own process: what it may see, how it ends, and what it costs the worker.

Also runs natively on Windows (ci-instance-identity.yml), so it imports only the probe.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.db import backend_probe

# Set by the Windows job, which starts the runner's own PostgreSQL.
_DSN_ENV = "BACKEND_PROBE_TEST_DSN"
_SECRET = "not-a-real-password-7f3a"


@pytest.fixture
def silent_server() -> Iterator[int]:
    """Accepts TCP and never says a word: what a dead network looks like from here."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield server.getsockname()[1]


def _silent_kwargs(port: int) -> dict[str, Any]:
    return {"host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": port, "user": "nobody",
            "password": _SECRET, "dbname": "nothing", "connect_timeout": 60, "sslmode": "disable"}


def _gone(pid: int) -> bool:
    """On Linux a zombie still has a /proc entry, so this also proves it was reaped."""
    if os.name == "nt":
        return True  # Popen.returncode is the evidence there.
    return not Path(f"/proc/{pid}").exists()


def strand_replies(
    probe: backend_probe.BackendProbe, monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[subprocess.Popen], list[socket.socket]]:
    """Each child's pipe number really becomes a blocking socket just before the real read."""
    children: list[subprocess.Popen] = []
    peers: list[socket.socket] = []
    real_start, real_read = probe._start, os.read

    def start(seconds: float, *files: str) -> subprocess.Popen:
        children.append(real_start(seconds, *files))
        return children[-1]

    def read(fd: int, size: int) -> bytes:
        child = children[-1] if children else None
        if child and child.stdout and not child.stdout.closed and fd == child.stdout.fileno():
            stranger, peer = socket.socketpair()
            os.dup2(stranger.fileno(), fd)
            stranger.close()
            peers.append(peer)
        return real_read(fd, size)

    monkeypatch.setattr(probe, "_start", start)
    monkeypatch.setattr(os, "read", read)
    return children, peers


def _read_within(probe: backend_probe.BackendProbe, port: int, seconds: float, bound: float) -> str:
    """The exception's name; a read that never returns fails here and cannot hang the suite."""
    outcome: list[str] = []

    def run() -> None:
        try:
            probe.read([1], (), _silent_kwargs(port), seconds)
        except Exception as exc:  # noqa: BLE001 - the caller asserts on it
            outcome.append(type(exc).__name__)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(bound)
    assert outcome, "read() must return even with its pipe's read stranded"
    return outcome[0]


@pytest.mark.parametrize("reply", [
    '{"rows": {}}', '{"rows": ""}', '{"rows": null}', '{"rows": 0}', '{"rows": false}',
    '[]', '""', '7', 'null', 'not json', '{"rows": []} {"rows": []}',
    '{"rows": [[1]]}', '{"rows": [{}]}', '{"rows": [""]}', '{"rows": [null]}',
    '{"rows": [[1, "idle", null, null, "tag"]]}',
    '{"rows": [[1, "idle", null, null, "tag", 0.0, 0]]}',
    '{"rows": [["1", "idle", null, null, "tag", 0.0]]}',
    '{"rows": [[true, "idle", null, null, "tag", 0.0]]}',
    '{"rows": [[1.0, "idle", null, null, "tag", 0.0]]}',
    '{"rows": [[2, "idle", null, null, "tag", 0.0]]}',
    '{"rows": [[1, 5, null, null, "tag", 0.0]]}',
    '{"rows": [[1, "idle", 5, null, "tag", 0.0]]}',
    '{"rows": [[1, "idle", null, [], "tag", 0.0]]}',
    '{"rows": [[1, "idle", null, null, {}, 0.0]]}',
    '{"rows": [[1, "idle", null, null, "tag", "90"]]}',
    '{"rows": [[1, "idle", null, null, "tag", true]]}',
    '{"rows": [[1, "idle", null, null, "tag", Infinity]]}',
    '{"rows": [[1, "idle", null, null, "tag", NaN]]}',
])
def test_a_malformed_reply_is_no_answer_not_an_exit(reply: str, tmp_path: Path, monkeypatch) -> None:
    """Through the real file transport. Only a complete answer may confirm an exit."""
    script = tmp_path / "child.py"
    script.write_text(f"import sys\nwith open(sys.argv[3], 'r+b') as f: f.write({reply!r}.encode())\n")
    monkeypatch.setattr(backend_probe, "_CHILD", script)
    with pytest.raises(backend_probe.NoAnswer):
        backend_probe.BackendProbe().read([1], (), {}, 15)


def test_a_silent_server_ends_in_a_kill_and_a_reap(silent_server: int) -> None:
    probe = backend_probe.BackendProbe()
    seen: list[subprocess.Popen] = []
    real_start = probe._start
    probe._start = lambda *a: seen.append(real_start(*a)) or seen[-1]  # type: ignore
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        probe.read([1], (), _silent_kwargs(silent_server), 1.0)
    assert time.monotonic() - started < 4
    assert seen[0].returncode not in (None, 0)
    assert _gone(seen[0].pid)
    assert probe._child is None


def test_credentials_reach_the_child_only_on_its_private_channel(
    silent_server: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_API_KEY", "keep-this-from-the-child")
    monkeypatch.setenv("PGSSLMODE", "disable")
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, **kwargs: Any) -> None:
            captured.update(argv=args, **kwargs)
            source, reply = map(Path, args[-2:])
            assert json.loads(source.read_bytes())["kwargs"]["password"] == _SECRET
            if os.name != "nt":
                assert source.stat().st_mode & 0o777 == 0o600
                assert reply.stat().st_mode & 0o777 == 0o600
                assert source.parent.stat().st_mode & 0o777 == 0o700
            else:
                # Python 3.12.10+ gives mode-0700 mkdir an owner/SYSTEM/admin ACL.
                paths = ",".join("'" + str(p).replace("'", "''") + "'"
                                 for p in (source.parent, source, reply))
                script = (
                    f"foreach ($path in @({paths})) {{ $a = Get-Acl -LiteralPath $path; "
                    "$owner = $a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
                    "$a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | "
                    "ForEach-Object { if ($_.IdentityReference.Value -notin "
                    "@($owner,'S-1-5-18','S-1-5-32-544')) { throw 'Non-private ACL' } } }"
                )
                acl = real_popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, error = acl.communicate(timeout=10)
                assert acl.returncode == 0, error.decode(errors="replace")
            super().__init__(args, **kwargs)
            if sys.platform == "linux":
                captured["cmdline"] = Path(f"/proc/{self.pid}/cmdline").read_bytes()

    monkeypatch.setattr(subprocess, "Popen", Spy)
    with pytest.raises(TimeoutError):
        seconds = 5 if os.name == "nt" else 0.5
        backend_probe.BackendProbe().read([1], (), _silent_kwargs(silent_server), seconds)
    assert _SECRET not in " ".join(captured["argv"])
    assert _SECRET.encode() not in captured.get("cmdline", b"")
    assert _SECRET not in json.dumps(captured["env"])
    assert "SOME_API_KEY" not in captured["env"]
    assert captured["env"].get("PGSSLMODE") == "disable", "libpq must connect like the pool does"
    assert not any(captured.get(key) == subprocess.PIPE for key in ("stdin", "stdout", "stderr"))
    for filename in captured["argv"][-2:]:
        assert not Path(filename).exists(), "request and answer removed after timeout"
    assert not Path(captured["argv"][-1]).parent.exists()
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW


@pytest.mark.skipif(os.name == "nt", reason="replaces a raw socket descriptor")
def test_returned_probe_cannot_close_a_reused_descriptor(monkeypatch) -> None:
    """Reuse AFTER read returns, then wake the old read: it must have no authority left."""
    monkeypatch.setattr(backend_probe, "_REAP_SECONDS", 0.1)
    probe = backend_probe.BackendProbe()
    children, peers = strand_replies(probe, monkeypatch)
    replacement, unrelated_peer = socket.socketpair()
    try:
        with socket.create_server(("127.0.0.1", 0)) as refusing:
            port = refusing.getsockname()[1]
        assert _read_within(probe, port, 5, 8) == "NoAnswer"
        if peers:  # Old pipe transport: its reader is still in the kernel.
            fd = children[0].stdout.fileno()
            os.dup2(replacement.fileno(), fd)
            replacement.close()
            replacement = socket.socket(fileno=fd)
            readers = [t for t in threading.enumerate() if t.name == "db-hold-check-pipe"]
            for peer in peers:
                peer.close()
            for reader in readers:
                reader.join(3)
            assert not any(t.is_alive() for t in readers)
        else:
            # No pipe now exists. Still reuse a number after return, then check it.
            fd = os.dup(replacement.fileno())
            os.close(fd)
            os.dup2(replacement.fileno(), fd)
            replacement.close()
            replacement = socket.socket(fileno=fd)
        replacement.sendall(b"still mine")
        assert unrelated_peer.recv(10) == b"still mine"
    finally:
        for peer in peers:
            peer.close()
        # A failing old implementation has already closed this number.
        try:
            replacement.close()
        except OSError:
            pass
        unrelated_peer.close()


def test_null_identity_is_no_answer() -> None:
    with pytest.raises(backend_probe.NoAnswer):
        backend_probe._parse(b'{"rows": [[1, "active", null, null, null, 0]]}', [1])


@pytest.mark.parametrize("shutdown", [False, True])
def test_only_one_lifecycle_owner_can_signal_or_reap(silent_server, monkeypatch, shutdown) -> None:
    """Include close(): it requests shutdown, it must not signal alongside the reaper."""
    owners = set()
    for name in ("poll", "wait", "kill"):
        original = getattr(subprocess.Popen, name)

        def observed(self, *args, _original=original, **kwargs):
            owners.add(threading.get_ident())
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(subprocess.Popen, name, observed)
    probe = backend_probe.BackendProbe()
    outcome = []
    caller = threading.Thread(target=lambda: outcome.append(_read_within(probe, silent_server, 0.2, 5)))
    caller.start()
    if shutdown:
        deadline = time.monotonic() + 3
        while probe._child is None and time.monotonic() < deadline:
            time.sleep(0.001)
        assert probe._child is not None
        probe.close()
    caller.join(6)
    assert outcome == ["TimeoutError"]
    assert len(owners) == 1, "a reaper can otherwise free the PID while another owner signals"




@pytest.mark.parametrize("outcome", ["success", "malformed", "process", "thread"])
def test_private_files_are_removed_on_every_exit(outcome, tmp_path, monkeypatch) -> None:
    paths = []
    real_directory = backend_probe.tempfile.TemporaryDirectory

    def directory(*args, **kwargs):
        result = real_directory(*args, dir=tmp_path, **kwargs)
        paths.append(Path(result.name))
        return result

    monkeypatch.setattr(backend_probe.tempfile, "TemporaryDirectory", directory)
    script = tmp_path / "child.py"
    reply = '{"rows": []}' if outcome == "success" else "not json"
    script.write_text(f"import sys\nwith open(sys.argv[3], 'r+b') as f: f.write({reply!r}.encode())\n")
    monkeypatch.setattr(backend_probe, "_CHILD", script)
    if outcome in ("process", "thread"):
        def refuse(*args, **kwargs):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(subprocess if outcome == "process" else threading.Thread,
                            "Popen" if outcome == "process" else "start", refuse)
    probe = backend_probe.BackendProbe()
    try:
        if outcome == "success":
            assert probe.read([1], (), {"password": _SECRET}, 2) == {}
        else:
            with pytest.raises((backend_probe.NoAnswer, RuntimeError)):
                probe.read([1], (), {"password": _SECRET}, 2)
    finally:
        probe.close()
    assert paths and not any(path.exists() for path in paths)


def test_delayed_launch_has_no_late_descriptor_work(tmp_path, monkeypatch) -> None:
    """Popen may take time, but its owner never holds a pipe or a temporary file handle."""
    probe = backend_probe.BackendProbe()
    real_start = probe._start
    entered, release = threading.Event(), threading.Event()
    files, children = [], []

    def delayed(seconds, *paths):
        files.extend(map(Path, paths))
        entered.set()
        assert release.wait(5)
        child = real_start(seconds, *paths)
        children.append(child)
        return child

    monkeypatch.setattr(probe, "_start", delayed)
    monkeypatch.setattr(backend_probe, "_REAP_SECONDS", 0.05)
    try:
        assert _read_within(probe, 1, 0.05, 1) == "TimeoutError"
        assert entered.is_set() and not any(path.exists() for path in files)
        with pytest.raises(backend_probe.NoAnswer, match="still running"):
            probe.read([1], (), {}, 0.05)
        probe.close()  # Also bounded while launch is stalled.
    finally:
        release.set()
        probe._owner.join(5)
    assert not probe._owner.is_alive()
    assert len(children) == 1 and children[0].returncode is not None
    assert _gone(children[0].pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn path")
def test_launch_has_no_internal_error_pipe_and_failures_have_no_slot_cap(monkeypatch) -> None:
    real_spawn = subprocess.Popen._posix_spawn
    spawned = []

    def spawn(self, *args, **kwargs):
        spawned.append(1)
        return real_spawn(self, *args, **kwargs)

    def no_read(*args):
        pytest.fail("launch or reply must not read a parent descriptor")

    monkeypatch.setattr(subprocess.Popen, "_posix_spawn", spawn)
    monkeypatch.setattr(os, "read", no_read)
    probe = backend_probe.BackendProbe()
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    for _ in range(10):
        with pytest.raises(backend_probe.NoAnswer, match="OperationalError"):
            probe.read([1], (), _silent_kwargs(port), 2)
    assert len(spawned) == 10
    assert probe._child is None


def test_a_refused_connection_reports_only_a_type_name() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    with pytest.raises(backend_probe.NoAnswer) as caught:
        backend_probe.BackendProbe().read([1], (), _silent_kwargs(port), 15)
    assert str(caught.value) == "OperationalError"


def test_a_child_that_outlives_its_kill_blocks_a_second_one_until_it_is_gone(
    silent_server: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = backend_probe.BackendProbe()
    real_kill = subprocess.Popen.kill
    real_start = subprocess.Popen.__init__
    spawned: list[int] = []

    def counting(self: Any, *args: Any, **kwargs: Any) -> None:
        real_start(self, *args, **kwargs)
        spawned.append(self.pid)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    monkeypatch.setattr(backend_probe, "_REAP_SECONDS", 0.1)
    with monkeypatch.context() as stuck:
        stuck.setattr(subprocess.Popen, "kill", lambda self: None)
        with pytest.raises(TimeoutError):
            probe.read([1], (), _silent_kwargs(silent_server), 0.3)
        assert probe._child is not None and probe._child.poll() is None
        with pytest.raises(backend_probe.NoAnswer, match="still running"):
            probe.read([1], (), _silent_kwargs(silent_server), 0.3)
        assert len(spawned) == 1, "never two at a time"
    leftover = probe._child
    assert subprocess.Popen.kill is real_kill
    deadline = time.monotonic() + 5
    while probe._child is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(TimeoutError):  # The same owner ended it; now a new one runs.
        probe.read([1], (), _silent_kwargs(silent_server), 0.3)
    assert leftover.poll() is not None
    assert len(spawned) == 2 and probe._child is None


def test_a_child_outliving_a_dead_parent_ends_by_itself(silent_server: int) -> None:
    """Nobody is left to kill it after a hard crash of the worker: its own limit does that."""
    request = json.dumps({
        "path": [str(Path(backend_probe.psycopg2.__file__).parents[1])], "pids": [1], "args": [],
        "kwargs": _silent_kwargs(silent_server),
    }).encode()
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        source, reply = Path(directory) / "request", Path(directory) / "reply"
        source.write_bytes(request)
        reply.write_bytes(b"\0" * backend_probe._MAX_ANSWER_BYTES)
        child = subprocess.Popen(
            [backend_probe._interpreter(), "-I", "-S", str(backend_probe._CHILD), "1",
             str(source), str(reply)], env=backend_probe._environment())
        child.wait(timeout=10)  # No kill from here, and it still ends.
        assert child.returncode != 0


def test_the_child_starts_from_a_venv_the_way_an_install_does(tmp_path: Path) -> None:
    """The driver answers from the venv's own packages, in the process a kill can reach."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    script = (
        "import sys, json\n"
        "sys.path[:0] = json.loads(sys.argv[1])\n"
        "from app.db import backend_probe\n"
        "print(backend_probe._interpreter())\n"
        "try:\n"
        "    backend_probe.BackendProbe().read([1], (), json.loads(sys.argv[2]), 30)\n"
        "except backend_probe.NoAnswer as exc:\n"
        "    print(exc)\n"
    )
    roots = [str(Path(backend_probe.__file__).parents[2]),
             str(Path(backend_probe.psycopg2.__file__).parents[1])]
    result = subprocess.run(
        [str(python), "-c", script, json.dumps(roots), json.dumps(_silent_kwargs(port))],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    interpreter, outcome = result.stdout.split()
    assert outcome == "OperationalError", "the child must import the driver and try to connect"
    if os.name == "nt":
        assert Path(interpreter).parent != python.parent, "the launcher would survive a kill"


@pytest.mark.skipif(_DSN_ENV not in os.environ, reason="needs a PostgreSQL to ask")
def test_the_child_starts_and_answers_from_this_install() -> None:
    """Native interpreter, -I -S, psycopg2's own libraries: what a desktop install runs."""
    dsn = os.environ[_DSN_ENV]
    assert backend_probe.BackendProbe().read([1], (dsn,), {}, 30) == {}
