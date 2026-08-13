"""Unit tests for app/core/event_gateway.py — the LISTEN/NOTIFY fan-out.

No real PostgreSQL: the :class:`EventGateway` is instantiated directly and its
listener thread is stubbed out, so only the in-memory registry + dispatch logic
is exercised.
"""

import asyncio
import time

import pytest

from app.core.event_gateway import EventGateway


@pytest.fixture
def gw(monkeypatch):
    g = EventGateway()
    # Never start the real listener thread (which would try to reach Postgres).
    monkeypatch.setattr(g, "_ensure_listener", lambda: None)
    return g


async def test_dispatch_only_reaches_matching_session(gw):
    q1 = gw.subscribe("s1")
    q2 = gw.subscribe("s1")
    q3 = gw.subscribe("s2")

    gw._dispatch("s1", 5)
    await asyncio.sleep(0.01)  # let call_soon_threadsafe callbacks run

    assert q1.get_nowait() == 5
    assert q2.get_nowait() == 5
    assert q3.empty()


async def test_unsubscribe_removes_delivery_and_empties_key(gw):
    q1 = gw.subscribe("s1")
    q2 = gw.subscribe("s1")

    gw.unsubscribe("s1", q1)
    assert "s1" in gw._subscribers  # q2 still there
    gw.unsubscribe("s1", q2)
    assert "s1" not in gw._subscribers  # key dropped once empty

    gw._dispatch("s1", 9)
    await asyncio.sleep(0.01)
    assert q1.empty()
    assert q2.empty()


def test_dispatch_to_closed_loop_does_not_raise(gw):
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    loop.close()
    with gw._lock:
        gw._subscribers["s3"] = {(loop, queue)}

    # Must not raise; the dead subscriber is dropped.
    gw._dispatch("s3", 1)
    assert "s3" not in gw._subscribers


# ── Listener-thread connection handling ──────────────────────────────────────
# _listen_loop is driven synchronously (no thread): psycopg2.connect is patched
# and _sleep is used as the loop-exit lever.


def _drive_listen_loop(monkeypatch, gateway, connect_effects, max_sleeps=10):
    """Run _listen_loop with a scripted psycopg2.connect and capture calls.

    connect_effects: list of "fail" (raise OSError) or "success" (return a mock
    conn whose select() wakeup then raises to re-enter the retry path).
    Returns (connect_calls, record_error_mock).
    """
    from unittest.mock import MagicMock

    import app.core.event_gateway as eg

    connect_calls = []
    effects = list(connect_effects)

    def fake_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        effect = effects.pop(0) if effects else "fail"
        if effect == "fail":
            raise OSError('could not translate host name "Pwd@10.0.0.200"')
        conn = MagicMock()
        return conn

    # A successful connect enters the notify wait; make it blow up immediately
    # so the loop returns to the reconnect path instead of blocking.
    def fake_select(*args, **kwargs):
        raise RuntimeError("connection dropped")

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= max_sleeps:
            gateway._stop = True

    record = MagicMock()
    if not getattr(eg.settings, "postgres_password", None):
        monkeypatch.setattr(eg.settings, "postgres_password", "p")
    monkeypatch.setattr(eg.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(eg.select, "select", fake_select)
    monkeypatch.setattr(gateway, "_sleep", fake_sleep)
    monkeypatch.setattr("app.db.error_logs.record_error", record)
    gateway._listen_loop()
    return connect_calls, record


def test_listen_loop_connects_with_parsed_dsn_kwargs(monkeypatch, gw):
    """The listener must decompose the DSN like the pool does — a raw URL with
    a literal '@' in the password makes libpq mis-parse the host."""
    import app.core.event_gateway as eg

    monkeypatch.setattr(eg.settings, "postgres_host", "10.0.0.200")
    monkeypatch.setattr(eg.settings, "postgres_user", "aihub")
    monkeypatch.setattr(eg.settings, "postgres_password", "p@ssPwd")
    monkeypatch.setattr(eg.settings, "postgres_db", "aihub")
    calls, _ = _drive_listen_loop(monkeypatch, gw, ["fail"], max_sleeps=1)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ()  # no raw-DSN positional argument
    assert kwargs["host"] == "10.0.0.200"
    assert kwargs["password"] == "p@ssPwd"
    assert kwargs["dbname"] == "aihub"


def test_listener_reports_durable_error_once_per_failure_streak(monkeypatch, gw):
    calls, record = _drive_listen_loop(
        monkeypatch, gw, ["fail"] * 6, max_sleeps=6
    )
    assert len(calls) == 6
    # Fires exactly once, on the 3rd consecutive failure — not on every retry.
    assert record.call_count == 1
    message = record.call_args.args[1]
    assert "LISTEN" in message


def test_successful_connect_resets_failure_streak(monkeypatch, gw):
    # fail, fail, success (streak resets), then the dropped connection counts
    # as failure #1 — the streak never reaches 3, so no durable error.
    _, record = _drive_listen_loop(
        monkeypatch, gw, ["fail", "fail", "success"], max_sleeps=3
    )
    assert record.call_count == 0


# ── Deterministic shutdown (issue #622) ──────────────────────────────────────


def test_stop_joins_running_listener_thread_promptly(monkeypatch):
    """stop() must halt and join a REAL listener thread quickly, even while it
    is in the reconnect-backoff sleep.

    Regression for #622: the daemon listener must not outlive teardown. The
    interruptible backoff sleep (``_stop_event``) means stop() returns without
    waiting out a multi-second backoff hop."""
    import app.core.event_gateway as eg

    g = EventGateway()
    monkeypatch.setattr(eg.settings, "postgres_password", "p")

    # Every connect fails immediately, so the loop parks in the (interruptible)
    # backoff sleep between attempts.
    def fake_connect(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(eg.psycopg2, "connect", fake_connect)
    monkeypatch.setattr("app.db.error_logs.record_error", lambda *a, **k: None)

    g._ensure_listener()
    thread = g._thread
    assert thread is not None and thread.is_alive()

    t0 = time.monotonic()
    g.stop(join_timeout=5.0)
    elapsed = time.monotonic() - t0

    assert not thread.is_alive(), "listener thread did not stop"
    assert elapsed < 2.0, f"stop() took {elapsed:.2f}s — backoff sleep not interruptible"


def test_stop_is_idempotent_and_safe_when_never_started():
    """stop() on a gateway whose thread never started is a clean no-op."""
    g = EventGateway()
    g.stop()  # thread is None — must not raise
    g.stop()  # idempotent
