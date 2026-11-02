"""Retained, sanitized reasons for preview-access denials (#1002).

One request is checked at the listener and in ``enforce_limits``; ``grant`` reuses
that decision. A transient failure answers 403 or 503 by when it lands. These tests
inject failures to pin the log that tells those stages and causes apart, and
that the refusals stay closed. They do not establish what failed in CI.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import psycopg2
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.db import sessions
from app.serve import IngressApplication

SESSION = "sess-diagnostics"
LOCAL, REMOTE, PREVIEW = 8000, 8001, 8002
# Stands in for what a driver or OS error may quote; it must not reach the log.
SECRET_TEXT = "driver-message-marker-must-not-reach-the-log"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: {"id": session_id})
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    preview_access._denial_log.clear()
    yield
    preview_access._denial_log.clear()


@pytest.fixture
def denials(caplog):
    caplog.set_level(logging.WARNING, logger=preview_access.logger.name)

    def read():
        return [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("Preview access denied")]

    return read


def _field(line: str, name: str) -> str:
    return next(part.split("=", 1)[1] for part in line.split() if part.startswith(name + "="))


def _client():
    app = FastAPI()

    @app.get("/api/v1/models")
    def models(token: str = Depends(require_auth)):
        return {"auth": token}

    return TestClient(IngressApplication(app, LOCAL, REMOTE, PREVIEW),
                      base_url=f"http://172.17.0.2:{PREVIEW}", client=("172.17.0.2", 54321),
                      raise_server_exceptions=False)


def _signed_in_client():
    client = _client()
    client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    return client


def _fail_on_call(monkeypatch, target, name, failing_call, error):
    """Replace ``target.name`` so only its ``failing_call``-th call raises."""
    real = getattr(target, name)
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return calls


# ---------------------------------------------------------------------------
# Each stage's answer stays what it was, and now says why
# ---------------------------------------------------------------------------

def test_session_lookup_failing_at_ingress_refuses_and_names_the_stage(monkeypatch, denials):
    client = _signed_in_client()
    _fail_on_call(monkeypatch, sessions, "get_session", 1, psycopg2.OperationalError(SECRET_TEXT))

    response = client.get("/api/v1/models")

    assert response.status_code == 403
    [line] = denials()
    assert _field(line, "stage") == "ingress"
    assert _field(line, "reason") == "session_lookup_unavailable"
    assert _field(line, "ordinal") == "1"
    assert _field(line, "cookie_present") == "True"
    assert "OperationalError" in line


def test_session_lookup_failing_after_the_listener_is_a_503_with_its_ordinal(monkeypatch, denials):
    """The listener admitted the request and the limits could not then be
    checked. The next request needs no special handling."""
    client = _signed_in_client()
    calls = _fail_on_call(monkeypatch, sessions, "get_session", 2, psycopg2.OperationalError(SECRET_TEXT))

    response = client.get("/api/v1/models")

    assert response.status_code == 503
    assert calls["n"] == 2  # listener, enforce_limits: no retry, and auth stops there
    [line] = denials()
    assert _field(line, "stage") == "enforce_limits"
    assert _field(line, "reason") == "session_lookup_unavailable"
    assert _field(line, "ordinal") == "2"
    assert _field(line, "path") == "/api/v1/models"
    assert _field(line, "method") == "GET"

    assert client.get("/api/v1/models").status_code == 200


def test_grant_reuses_the_decision_instead_of_looking_again(monkeypatch, denials):
    """Once ``enforce_limits`` has answered, one later failure cannot change it."""
    client = _signed_in_client()
    calls = _fail_on_call(monkeypatch, sessions, "get_session", 3, psycopg2.OperationalError(SECRET_TEXT))

    assert client.get("/api/v1/models").status_code == 200
    assert calls["n"] == 2  # listener, enforce_limits
    assert denials() == []


def test_grant_without_a_decision_names_its_own_stage(monkeypatch, denials):
    app = FastAPI()

    @app.get("/api/v1/models")
    def models(request: Request):
        return {"granted": preview_access.grant(request) is not None}

    client = TestClient(IngressApplication(app, LOCAL, REMOTE, PREVIEW),
                        base_url=f"http://172.17.0.2:{PREVIEW}", client=("172.17.0.2", 54321))
    client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    _fail_on_call(monkeypatch, sessions, "get_session", 2, psycopg2.OperationalError(SECRET_TEXT))

    assert client.get("/api/v1/models").json() == {"granted": False}
    [line] = denials()
    assert _field(line, "stage") == "grant"
    assert _field(line, "reason") == "session_lookup_unavailable"
    assert _field(line, "ordinal") == "2"


def test_socket_settling_names_its_stage_and_still_raises(monkeypatch, denials):
    token = preview_access.mint(SESSION)
    conn = SimpleNamespace(
        cookies={preview_access.cookie_name(): token},
        scope={"type": "websocket", "path": "/api/v1/ws/chat"},
        state=SimpleNamespace(),
    )
    _fail_on_call(monkeypatch, sessions, "get_session", 1, psycopg2.OperationalError(SECRET_TEXT))

    with pytest.raises(psycopg2.OperationalError):
        preview_access.settle(conn)

    [line] = denials()
    assert _field(line, "stage") == "settle"
    assert _field(line, "reason") == "session_lookup_unavailable"
    assert _field(line, "method") == "WEBSOCKET"
    assert SECRET_TEXT not in line


def test_key_failing_in_enforce_limits_is_a_503_not_an_auth_failure(monkeypatch, denials):
    client = _signed_in_client()
    _fail_on_call(monkeypatch, preview_access, "_key", 2, OSError(9, SECRET_TEXT))

    response = client.get("/api/v1/models")

    assert response.status_code == 503
    [line] = denials()
    assert _field(line, "stage") == "enforce_limits"
    assert _field(line, "reason") == "key_unavailable"
    assert _field(line, "ordinal") == "2"
    assert "errno=EBADF" in line


def test_key_failing_at_each_stage_gives_that_stages_answer(monkeypatch, denials):
    """One transient key failure explains both answers, by when it lands."""
    client = _signed_in_client()
    for failing_call, status, stage in ((1, 403, "ingress"), (2, 503, "enforce_limits")):
        preview_access._denial_log.clear()
        with monkeypatch.context() as patch:
            _fail_on_call(patch, preview_access, "_key", failing_call, OSError(9, SECRET_TEXT))
            assert client.get("/api/v1/models").status_code == status
        assert _field(denials()[-1], "stage") == stage
        assert _field(denials()[-1], "reason") == "key_unavailable"


def test_stages_of_one_request_share_an_id_and_requests_do_not(monkeypatch, denials):
    client = _signed_in_client()
    monkeypatch.setattr(sessions, "get_session", lambda session_id: None)
    assert client.get("/api/v1/models").status_code == 403
    first = _field(denials()[-1], "request")
    preview_access._denial_log.clear()
    assert client.get("/api/v1/models").status_code == 403
    assert _field(denials()[-1], "request") != first

    # A listener-admitted request reaches enforce_limits with its id and count.
    monkeypatch.setattr(sessions, "get_session", lambda session_id: {"id": session_id})
    real, calls = sessions.get_session, {"n": 0}

    def fails_after_ingress(session_id):
        calls["n"] += 1
        if calls["n"] > 1:
            raise psycopg2.OperationalError(SECRET_TEXT)
        return real(session_id)

    monkeypatch.setattr(sessions, "get_session", fails_after_ingress)
    preview_access._denial_log.clear()
    assert client.get("/api/v1/models").status_code == 503
    assert _field(denials()[-1], "stage") == "enforce_limits"
    assert _field(denials()[-1], "ordinal") == "2"

    # Every stage of one request shares the id, including ones that denied nothing.
    scope = {"type": "http", "method": "GET", "path": "/api/v1/models"}
    one = preview_access._begin_check("enforce_limits", scope, {})
    two = preview_access._begin_check("grant", scope, {})
    assert one.request_id == two.request_id
    assert (one.ordinal, two.ordinal) == (1, 2)


# ---------------------------------------------------------------------------
# The cookie question the 403 could not answer
# ---------------------------------------------------------------------------

def test_listener_says_when_no_cookie_was_sent(denials):
    assert _client().get("/api/v1/models").status_code == 403
    [line] = denials()
    assert _field(line, "reason") == "missing_cookie"
    assert _field(line, "cookie_present") == "False"
    assert _field(line, "cookie_count") == "0"


def test_listener_tells_another_installs_cookie_from_none(denials):
    client = _client()
    client.cookies.set(preview_access.COOKIE_PREFIX + "another-install", preview_access.mint(SESSION))
    assert client.get("/api/v1/models").status_code == 403
    [line] = denials()
    assert _field(line, "reason") == "other_install_cookie"
    assert _field(line, "cookie_present") == "True"
    assert "another-install" not in line


def test_owner_without_a_preview_cookie_logs_nothing(denials):
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/api/v1/models", "query_string": b"",
                       "headers": [], "client": ("10.0.0.20", 1), "shuffle.ingress": "local"})
    preview_access.enforce_limits(request)
    assert preview_access.grant(request) is None
    assert denials() == []


# ---------------------------------------------------------------------------
# The verifier's reasons
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token_for, reason", [
    (lambda: "not-a-token", "malformed_token"),
    (lambda: "x" * 513, "malformed_token"),
    (lambda: preview_access.mint(SESSION)[:-1] + "é", "bad_signature"),
    (lambda: preview_access.mint(SESSION)[:-4] + "0000", "bad_signature"),
    (lambda: preview_access.mint(SESSION, 60, now=time.time() - 120), "expired_token"),
])
def test_token_refusals_name_their_reason(token_for, reason, denials):
    assert preview_access.verify(token_for()) is None
    [line] = denials()
    assert _field(line, "reason") == reason
    assert _field(line, "stage") == "verify"


def test_deleted_and_key_owned_sessions_are_told_apart(monkeypatch, denials):
    token = preview_access.mint(SESSION)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: None)
    assert preview_access.verify(token) is None
    monkeypatch.setattr(sessions, "get_session", lambda session_id: {"id": session_id, "api_user_id": "key-1"})
    assert preview_access.verify(token) is None
    assert [_field(line, "reason") for line in denials()] == ["session_missing", "session_not_owner"]


def test_unreadable_key_still_raises_from_verify(monkeypatch, denials):
    token = preview_access.mint(SESSION)

    def broken(*args):
        raise OSError(13, SECRET_TEXT)

    monkeypatch.setattr(preview_access, "_key", broken)
    with pytest.raises(OSError):
        preview_access.verify(token)
    assert _field(denials()[-1], "reason") == "key_unavailable"
    assert "errno=EACCES" in denials()[-1]


def test_nothing_presented_is_not_a_denial(denials):
    assert preview_access.verify(None) is None
    assert preview_access.verify("") is None
    assert denials() == []


# ---------------------------------------------------------------------------
# What may and may not reach the log
# ---------------------------------------------------------------------------

def test_log_carries_no_credentials_or_error_text(monkeypatch, denials):
    client = _signed_in_client()
    token = client.cookies.get(preview_access.cookie_name())
    cookie_name = preview_access.cookie_name()
    error = psycopg2.OperationalError(SECRET_TEXT)
    _fail_on_call(monkeypatch, sessions, "get_session", 1, error)
    assert client.get("/api/v1/models?token=" + token).status_code == 403

    [line] = denials()
    for forbidden in (token, cookie_name, SECRET_TEXT, "token="):
        assert forbidden not in line


def test_session_lookup_error_reports_sqlstate_but_not_arbitrary_codes():
    class Coded(Exception):
        pgcode = "53300"

    class Forged(Exception):
        pgcode = "forged-code-marker"

    assert "sqlstate=53300" in preview_access.describe_error(Coded(SECRET_TEXT))
    described = preview_access.describe_error(Forged(SECRET_TEXT))
    assert "forged-code-marker" not in described and "sqlstate" not in described


def test_denied_path_stays_on_one_line(monkeypatch, denials):
    client = _client()
    assert client.get("/api/v1/mo%0Adels%20x").status_code == 403
    [line] = denials()
    assert "\n" not in line
    assert _field(line, "path") == "/api/v1/mo?dels?x"


# ---------------------------------------------------------------------------
# Volume, and diagnostics never deciding anything
# ---------------------------------------------------------------------------

def test_repeated_denials_are_counted_not_repeated(monkeypatch, denials):
    token = preview_access.mint(SESSION)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: None)
    clock = [1000.0]
    monkeypatch.setattr(preview_access, "_monotonic", lambda: clock[0])

    for _ in range(3):
        preview_access.verify(token)
    assert len(denials()) == 1

    clock[0] += preview_access._DENIAL_LOG_WINDOW_S
    preview_access.verify(token)
    assert len(denials()) == 2
    assert _field(denials()[-1], "suppressed") == "2"


def test_an_outage_stays_visible_past_the_expected_denial_window(monkeypatch, denials):
    token = preview_access.mint(SESSION)

    def down(session_id):
        raise psycopg2.OperationalError(SECRET_TEXT)

    monkeypatch.setattr(sessions, "get_session", down)
    clock = [1000.0]
    monkeypatch.setattr(preview_access, "_monotonic", lambda: clock[0])
    with pytest.raises(psycopg2.OperationalError):
        preview_access.verify(token)
    clock[0] += preview_access._UNAVAILABLE_LOG_WINDOW_S
    with pytest.raises(psycopg2.OperationalError):
        preview_access.verify(token)
    assert len(denials()) == 2


def test_a_failing_log_call_cannot_change_a_decision(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("log handler failed")

    monkeypatch.setattr(preview_access.logger, "warning", broken)
    token = preview_access.mint(SESSION)
    assert preview_access.verify(token).session_id == SESSION
    monkeypatch.setattr(sessions, "get_session", lambda session_id: None)
    assert preview_access.verify(token) is None


# ---------------------------------------------------------------------------
# The listener could not even start its check
# ---------------------------------------------------------------------------

def _dispatch_fails(monkeypatch):
    """No worker thread can start: the check itself never runs."""
    from app.db import async_database

    async def run_sync(*args, **kwargs):
        raise RuntimeError(SECRET_TEXT)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the fallback must not read the key or the database")

    monkeypatch.setattr(async_database, "run_sync", run_sync)
    monkeypatch.setattr(preview_access, "_key", must_not_run)
    monkeypatch.setattr(sessions, "get_session", must_not_run)


def test_repeated_dispatch_failures_share_the_limiter_and_stay_correlated(monkeypatch, denials):
    clock = [1000.0]
    monkeypatch.setattr(preview_access, "_monotonic", lambda: clock[0])
    client = _signed_in_client()
    _dispatch_fails(monkeypatch)

    for _ in range(3):
        assert client.get("/api/v1/models").status_code == 403

    [line] = denials()
    assert _field(line, "stage") == "ingress"
    assert _field(line, "reason") == "check_not_started"
    assert _field(line, "ordinal") == "1"
    assert _field(line, "request") != "-"
    assert _field(line, "path") == "/api/v1/models"
    assert _field(line, "cookie_present") == "True"
    assert "RuntimeError" in line
    assert SECRET_TEXT not in line

    clock[0] += preview_access._UNAVAILABLE_LOG_WINDOW_S
    assert client.get("/api/v1/models").status_code == 403
    assert _field(denials()[-1], "suppressed") == "2"


@pytest.mark.parametrize("broken", ["_begin_check", "_note_denial"])
def test_dispatch_failure_stays_a_403_when_its_diagnostic_fails(monkeypatch, broken):
    def boom(*args, **kwargs):
        raise OSError(9, SECRET_TEXT)

    client = _signed_in_client()
    _dispatch_fails(monkeypatch)
    monkeypatch.setattr(preview_access, broken, boom)

    assert client.get("/api/v1/models").status_code == 403


def test_dispatch_failure_stays_a_403_when_logging_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(9, SECRET_TEXT)

    client = _signed_in_client()
    _dispatch_fails(monkeypatch)
    monkeypatch.setattr(preview_access.logger, "warning", boom)

    assert client.get("/api/v1/models").status_code == 403
