"""Real child ownership and mailbox publication; also runs in native Windows CI."""
import json
import mmap
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import Mock

import pytest

from app.core import event_listener as listener


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "listener deadline expired"
        time.sleep(.01)


@pytest.fixture
def silent_kwargs():
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield {"host": "127.0.0.1", "port": server.getsockname()[1], "dbname": "nothing",
               "user": "nobody", "password": "not-a-real-password", "connect_timeout": 60,
               "sslmode": "disable"}


def test_child_receives_no_database_or_provider_environment(tmp_path, monkeypatch, silent_kwargs):
    keys = ["PGPASSWORD", "PGSERVICE", "PGPASSFILE", "DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
    for key in keys:
        monkeypatch.setenv(key, "not-a-real-secret-sentinel")
    receipt = tmp_path / "environment.json"
    source = Path(listener.__file__).read_text(encoding="utf-8")
    observation = (f"\n    Path({str(receipt)!r}).write_text(json.dumps(" +
                   f"{{key: key in os.environ for key in {keys!r}}}))")
    source = source.replace('    sys.path.extend(request["path"])',
                            '    sys.path.extend(request["path"])' + observation)
    child_source = tmp_path / "observed_listener.py"
    child_source.write_text(source, encoding="utf-8")
    monkeypatch.setattr(listener, "_CHILD", child_source)
    child = listener.NotificationListener(silent_kwargs)
    process = child._child
    try:
        until(receipt.exists)
        assert json.loads(receipt.read_text()) == dict.fromkeys(keys, False)
    finally:
        child.close()
    assert process.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="kernel Windows Job containment")
def test_parent_death_kills_the_real_listener_even_when_interpreter_suspended(tmp_path):
    import psutil
    import psycopg2.extensions

    dsn = os.environ.get("BACKEND_PROBE_TEST_DSN")
    assert dsn, "native CI must supply the runner's PostgreSQL DSN"
    kwargs = psycopg2.extensions.parse_dsn(dsn)
    if os.environ.get("PGPASSWORD"):
        kwargs["password"] = os.environ["PGPASSWORD"]
    receipt = tmp_path / "child.json"
    script = textwrap.dedent("""
        import json, sys, time
        from pathlib import Path
        from app.core.event_listener import NotificationListener
        request = json.load(sys.stdin)
        child = NotificationListener(request["kwargs"])
        deadline = time.monotonic() + 10
        while not child.connected:
            child.read()
            assert time.monotonic() < deadline
            time.sleep(.01)
        Path(request["receipt"]).write_text(json.dumps({"pid": child._child.pid}))
        time.sleep(60)
    """)
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    process = None
    try:
        parent.stdin.write(json.dumps({"kwargs": kwargs, "receipt": str(receipt)}))
        parent.stdin.close()
        until(receipt.exists, 12)
        process = psutil.Process(json.loads(receipt.read_text())["pid"])
        process.suspend()  # Stronger than a driver freeze: no Python thread can run.
        parent.kill()
        parent.wait(timeout=3)
        process.wait(timeout=3)  # Must be killed by the OS, not its heartbeat loop.
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if process is not None and process.is_running():
            process.kill()
            process.wait(timeout=3)




def test_windows_admission_attaches_both_pinned_handles_before_opening_gate(monkeypatch):
    from contextlib import contextmanager
    from app.core.transcript_views import child_process, windows_process

    memory = mmap.mmap(-1, listener._SIZE)
    child = object.__new__(listener.NotificationListener)
    child._memory, child._child, child._jobs = memory, Mock(pid=123), []
    child._child.poll.return_value = None
    api = Mock()
    api.assign_kill_on_close_job_handle.side_effect = [101, 102]
    monkeypatch.setattr(child_process, "_job_object", lambda: api)

    @contextmanager
    def pinned(launcher_pid, runtime_pid):
        assert (launcher_pid, runtime_pid) == (123, 456)
        yield [(123, 111), (456, 222)]
        assert listener._number(memory, 32) == 1

    monkeypatch.setattr(windows_process, "pinned_processes", pinned)
    try:
        listener._store(memory, 24, 456)
        child._admit_windows()
        assert child._jobs == [101, 102]
        assert api.assign_kill_on_close_job_handle.call_args_list == [((111,),), ((222,),)]
    finally:
        memory.close()


def test_concurrent_writer_lapping_a_reader_never_publishes_torn_payload(monkeypatch):
    memory = mmap.mmap(-1, listener._SIZE)
    child = object.__new__(listener.NotificationListener)
    child._memory, child._child = memory, Mock()
    child._child.poll.return_value = None
    child._seen, child._started = 0, time.monotonic()
    listener._store(memory, 8, time.monotonic_ns())
    listener._publish(memory, 1, json.dumps({"sequence": 1, "text": "☃" * 1000}))
    checked, published = threading.Event(), threading.Event()
    offset = listener._HEADER_BYTES + listener._SLOT_BYTES
    original = listener._number
    outcome, errors = [], []

    def number(mapping, position):
        value = original(mapping, position)
        if position == offset and not checked.is_set():
            checked.set()
            assert published.wait(3)
        return value

    monkeypatch.setattr(listener, "_number", number)

    def read():
        try:
            outcome.append(child.read())
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert checked.wait(3)
        for seq in range(2, listener._SLOTS * 2 + 2):
            listener._publish(memory, seq, json.dumps({"sequence": seq, "text": "新" * (seq % 1000)}))
        published.set()
        reader.join(3)
        assert not reader.is_alive() and not errors
        assert outcome == [([], True)]  # A gap, never the overwritten slot's payload.
        payloads, overflow = child.read()
        assert overflow and len(payloads) == listener._SLOTS
        assert json.loads(payloads[-1])["sequence"] == listener._SLOTS * 2 + 1
    finally:
        published.set()
        reader.join(3)
        memory.close()
