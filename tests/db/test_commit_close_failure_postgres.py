"""A failed close of the deadline's private descriptor is not a failed commit (#1002).

A preview run logged ``session_lookup_unavailable`` with ``OSError EBADF`` raised
from ``disarm``: the lookup's read had finished, then closing the watchdog's
private duplicate failed and replaced that result. These tests close the
duplicate behind the watchdog on purpose. That is injected, so it proves the path
from a closed descriptor to each refusal; it does not identify what closed the
descriptor in that run.
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import time

import psycopg2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.preferences import require_preference_write
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.db import base, commit_deadline, sessions
from app.serve import IngressApplication
from tests.db import schema_lifecycle_helpers as lifecycle
from tests.db.test_commit_deadline_postgres import private_db, private_pool  # noqa: F401

SESSION = "sess-commit-close"
LOCAL, REMOTE, PREVIEW = 8000, 8001, 8002


def _close_behind(sock: socket.socket) -> None:
    """What a double close elsewhere does: the number goes, the object still has it."""
    os.close(sock.fileno())


@pytest.fixture(autouse=True)
def _quiet_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit_deadline, "_close_failure_log", (None, 0))


@pytest.fixture
def stray_close_on_arm(monkeypatch: pytest.MonkeyPatch):
    """Close the duplicate behind the watchdog on the ``target``-th arm only."""
    real = commit_deadline._watchdog.arm
    state = {"arms": 0, "closed": 0, "target": None}

    def arm(fd: int, seconds: float) -> socket.socket:
        sock = real(fd, seconds)
        state["arms"] += 1
        if state["arms"] == state["target"]:
            _close_behind(sock)
            state["closed"] += 1
        return sock

    monkeypatch.setattr(commit_deadline._watchdog, "arm", arm)
    return state


# ---------------------------------------------------------------------------
# The commit's own outcome survives
# ---------------------------------------------------------------------------

def test_stray_close_leaves_a_finished_commit_committed(private_pool, stray_close_on_arm) -> None:
    with base.get_db() as conn:
        conn.cursor().execute("CREATE TABLE IF NOT EXISTS close_probe (id int PRIMARY KEY)")
        conn.cursor().execute("TRUNCATE close_probe")

    stray_close_on_arm.update(arms=0, target=1)
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO close_probe VALUES (1)")
    assert stray_close_on_arm["closed"] == 1

    # Durable, and the connection and its pool slot stay in service.
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM close_probe")
        assert cursor.fetchall() == [{"id": 1}]


def test_stray_close_does_not_hide_a_real_commit_error(private_pool, stray_close_on_arm) -> None:
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS close_parent (id int PRIMARY KEY)")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS close_child (parent int REFERENCES close_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)")

    stray_close_on_arm.update(arms=0, target=1)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with base.get_db() as conn:
            conn.cursor().execute("INSERT INTO close_child VALUES (404)")
    assert stray_close_on_arm["closed"] == 1


def test_stray_close_before_expiry_still_reports_the_deadline(private_db, monkeypatch) -> None:
    conn = lifecycle.pg_connect(private_db)
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.05)
    real_arm = commit_deadline._watchdog.arm

    def arm(fd: int, seconds: float) -> socket.socket:
        sock = real_arm(fd, seconds)
        _close_behind(sock)
        return sock

    monkeypatch.setattr(commit_deadline._watchdog, "arm", arm)
    with pytest.raises(psycopg2.OperationalError, match="commit deadline exceeded"):
        with commit_deadline.bounded_commit(conn):
            time.sleep(0.3)
    assert conn.closed


# ---------------------------------------------------------------------------
# The deadline keeps working, and the failure stays visible
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="closes a raw socket descriptor by number")
def test_watchdog_survives_a_close_failure_at_expiry() -> None:
    """Otherwise its one thread dies and every later commit runs without a deadline."""
    watchdog = commit_deadline._CommitWatchdog()
    left, right = socket.socketpair()
    try:
        first = watchdog.arm(left.fileno(), 0.01)
        _close_behind(first)
        time.sleep(0.2)
        assert watchdog.disarm(first) is True

        second = watchdog.arm(left.fileno(), 0.01)
        time.sleep(0.2)
        assert watchdog.disarm(second) is True
    finally:
        left.close()
        right.close()


@pytest.mark.skipif(os.name == "nt", reason="closes a raw socket descriptor by number")
def test_close_failures_are_logged_sanitized_and_rate_limited(caplog, monkeypatch) -> None:
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    watchdog = commit_deadline._CommitWatchdog()
    left, right = socket.socketpair()
    try:
        for _ in range(3):
            sock = watchdog.arm(left.fileno(), 5.0)
            _close_behind(sock)
            assert watchdog.disarm(sock) is False
    finally:
        left.close()
        right.close()
    lines = [r.getMessage() for r in caplog.records if "descriptor" in r.getMessage()]
    assert len(lines) == 1
    assert f"errno={errno.errorcode[errno.EBADF]}" in lines[0]

    last, suppressed = commit_deadline._close_failure_log
    assert suppressed == 2
    # After the window the next one logs and reports what it held back.
    monkeypatch.setattr(commit_deadline, "_close_failure_log", (last - 3600, suppressed))
    commit_deadline._report_close_failure(errno.EBADF)
    lines = [r.getMessage() for r in caplog.records if "descriptor" in r.getMessage()]
    assert len(lines) == 2 and "suppressed=2" in lines[1]


def test_a_healthy_commit_logs_nothing(private_pool, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    with base.get_db() as conn:
        conn.cursor().execute("SELECT 1")
    assert not [r for r in caplog.records if r.name == commit_deadline.logger.name]


# ---------------------------------------------------------------------------
# The real lookup, through the real listener and auth
# ---------------------------------------------------------------------------

@pytest.fixture
def preview_app(private_pool, tmp_path, monkeypatch):
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id text PRIMARY KEY, api_user_id text, "
            "is_worker boolean DEFAULT false, parent_session_id text)")
        cursor.execute("INSERT INTO sessions (id) VALUES (%s) ON CONFLICT DO NOTHING", (SESSION,))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    preview_access._denial_log.clear()

    app = FastAPI()
    writes: list[str] = []

    @app.get("/api/v1/models")
    def models(token: str = Depends(require_auth)):
        return {"auth": token}

    @app.get("/api/v1/agents")
    def agents(token: str = Depends(require_auth)):
        return {"auth": token}

    @app.put("/api/v1/preferences/{key}")
    def write(key: str, token: str = Depends(require_preference_write)):
        writes.append(key)
        return {"key": key}

    client = TestClient(IngressApplication(app, LOCAL, REMOTE, PREVIEW),
                        base_url=f"http://172.17.0.2:{PREVIEW}", client=("172.17.0.2", 54321),
                        raise_server_exceptions=False)
    client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    yield client, writes
    preview_access._denial_log.clear()


@pytest.mark.skipif(os.name == "nt", reason="closes a raw socket descriptor by number")
@pytest.mark.parametrize("path", ["/api/v1/models", "/api/v1/agents"])
@pytest.mark.parametrize("lookup", [1, 2, 3], ids=["ingress", "enforce_limits", "grant"])
def test_stray_close_during_any_lookup_does_not_refuse_a_read(
    preview_app, stray_close_on_arm, path: str, lookup: int,
) -> None:
    """Before the fix: ingress answered 403 (the agents symptom), grant 401 (the
    models symptom). enforce_limits answered 200, having lost its ceiling."""
    client, _ = preview_app
    stray_close_on_arm.update(arms=0, target=lookup)

    response = client.get(path)

    assert stray_close_on_arm["closed"] == 1
    assert response.status_code == 200


@pytest.mark.skipif(os.name == "nt", reason="closes a raw socket descriptor by number")
@pytest.mark.parametrize("lookup", [1, 2, 3, 4], ids=["ingress", "enforce_limits", "grant", "write_guard"])
def test_stray_close_during_any_lookup_keeps_preview_writes_refused(
    preview_app, stray_close_on_arm, lookup: int,
) -> None:
    """Before the fix the fourth lookup, in the write guard, let the write through."""
    client, writes = preview_app
    stray_close_on_arm.update(arms=0, target=lookup)

    response = client.put("/api/v1/preferences/composer.modelFast.claude-opus-5",
                          headers={"Origin": f"http://172.17.0.2:{PREVIEW}"})

    assert stray_close_on_arm["closed"] == 1
    assert response.status_code == 403
    assert writes == []


def test_a_revoked_session_is_still_refused_at_every_new_request(preview_app) -> None:
    client, _ = preview_app
    assert client.get("/api/v1/models").status_code == 200
    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM sessions WHERE id = %s", (SESSION,))
    try:
        assert client.get("/api/v1/models").status_code == 403
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (SESSION,))
