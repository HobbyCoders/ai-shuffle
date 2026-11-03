"""The probe's own process: what it may see, how it ends, and what it costs the worker.

Also runs natively on Windows (ci-instance-identity.yml), so it imports only the probe.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
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


def test_a_silent_server_ends_in_a_kill_and_a_reap(silent_server: int) -> None:
    probe = backend_probe.BackendProbe()
    seen: list[subprocess.Popen] = []
    real_start = probe._start
    probe._start = lambda seconds: seen.append(real_start(seconds)) or seen[-1]  # type: ignore
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
            super().__init__(args, **kwargs)
            if sys.platform == "linux":
                captured["cmdline"] = Path(f"/proc/{self.pid}/cmdline").read_bytes()

    monkeypatch.setattr(subprocess, "Popen", Spy)
    with pytest.raises(TimeoutError):
        backend_probe.BackendProbe().read([1], (), _silent_kwargs(silent_server), 0.5)
    assert _SECRET not in " ".join(captured["argv"])
    assert _SECRET.encode() not in captured.get("cmdline", b"")
    assert _SECRET not in json.dumps(captured["env"])
    assert "SOME_API_KEY" not in captured["env"]
    assert captured["env"].get("PGSSLMODE") == "disable", "libpq must connect like the pool does"
    assert captured["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW


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
    with pytest.raises(TimeoutError):  # Killable again: it is ended, and a new one runs.
        probe.read([1], (), _silent_kwargs(silent_server), 0.3)
    assert leftover.poll() is not None
    assert len(spawned) == 2 and probe._child is None


def test_a_child_outliving_a_dead_parent_ends_by_itself(silent_server: int) -> None:
    """Nobody is left to kill it after a hard crash of the worker: its own limit does that."""
    request = json.dumps({
        "path": [str(Path(backend_probe.psycopg2.__file__).parents[1])], "pids": [1], "args": [],
        "kwargs": _silent_kwargs(silent_server),
    }).encode()
    child = subprocess.Popen(
        [backend_probe._interpreter(), "-I", "-S", str(backend_probe._CHILD), "1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=backend_probe._environment())
    child.communicate(request, timeout=10)  # No kill from here, and it still ends.
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
