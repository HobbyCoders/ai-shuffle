"""Negative paths of ``app.core.owner_auth``.

The owner boundary is the one place a request becomes the install owner
without a password, and the place the password itself is checked. These tests
pin what must be refused: hashes whose stored parameters would make a
verification expensive or meaningless, database errors that must not reopen
first-run setup or quietly enable LAN trust, requests that only *look* local
(proxied, rebinding, cross-site), and origins that downgrade TLS.
"""
import os
import secrets

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import desktop_listener, owner_auth
from app.core.config import settings
from app.db import database


def _request(*, host="192.168.1.10:8000", peer="192.168.1.50", scheme="http", ingress="local",
             headers=None, client=True):
    raw = [(b"host", host.encode())] if host is not None else []
    for name, value in (headers or {}).items():
        raw.append((name.lower().encode(), value.encode()))
    scope = {"type": "http", "method": "POST", "scheme": scheme, "path": "/",
             "server": ("0.0.0.0", 8000), "headers": raw, "query_string": b""}
    if client:
        scope["client"] = (peer, 50000)
    if ingress:
        scope["shuffle.ingress"] = ingress
    return Request(scope)


def _cheap_hash(password, memory=8, iterations=1, lanes=1):
    salt = secrets.token_bytes(16)
    key = owner_auth._argon2(password, salt, memory, iterations, lanes, 32)
    return (f"$argon2id$v=19$m={memory},t={iterations},p={lanes}"
            f"${owner_auth._b64(salt)}${owner_auth._b64(key)}")


# ---------------------------------------------------------------------------
# Password hashes
# ---------------------------------------------------------------------------

def test_verify_hash_accepts_the_right_password_only():
    encoded = _cheap_hash("correct horse battery")
    assert owner_auth.verify_hash("correct horse battery", encoded)
    assert not owner_auth.verify_hash("correct horse batterY", encoded)


@pytest.mark.parametrize("password,encoded", [
    (None, "$argon2id$v=19$m=8,t=1,p=1$AAAA$AAAA"),
    ("pw", None),
    ("pw", "$argon2i$v=19$m=8,t=1,p=1$AAAA$AAAA"),       # not argon2id
    ("pw", "$argon2id$v=18$m=8,t=1,p=1$AAAA$AAAA"),      # wrong version
    ("pw", "plaintext-password"),
    ("pw", "$argon2id$v=19$m=8,t=1,p=1$A$AAAA"),         # undecodable salt
])
def test_verify_hash_rejects_malformed_input(password, encoded):
    assert owner_auth.verify_hash(password, encoded) is False


@pytest.mark.parametrize("memory,iterations,lanes", [(4, 1, 1), (8, 17, 1), (8, 1, 65), (8, 0, 1)])
def test_verify_hash_refuses_out_of_range_parameters(monkeypatch, memory, iterations, lanes):
    """A stored hash cannot demand unbounded work (or none) from a login."""
    encoded = _cheap_hash("pw-long-enough")
    tampered = encoded.replace("m=8,t=1,p=1", f"m={memory},t={iterations},p={lanes}")
    assert tampered != encoded
    calls = []
    monkeypatch.setattr(owner_auth, "_argon2", lambda *a: calls.append(a) or b"")
    assert owner_auth.verify_hash("pw-long-enough", tampered) is False
    assert calls == []


def test_verify_password_without_stored_password_is_false_and_still_hashes(monkeypatch, mock_db):
    calls = []
    real = owner_auth.verify_hash
    monkeypatch.setattr(owner_auth, "_dummy", None)
    monkeypatch.setattr(owner_auth, "hash_password", lambda pw: _cheap_hash(pw))
    monkeypatch.setattr(owner_auth, "verify_hash", lambda pw, enc: calls.append(enc) or real(pw, enc))
    assert owner_auth.verify_password("anything-at-all") is False
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


@pytest.mark.parametrize("password,message", [
    ("short", "at least"),
    (None, "at least"),
    ("x" * (owner_auth.PASSWORD_MAX_LENGTH + 1), "at most"),
    (" " * owner_auth.PASSWORD_MIN_LENGTH, "only spaces"),
])
def test_validate_new_password(password, message):
    with pytest.raises(ValueError, match=message):
        owner_auth.validate_new_password(password)


# ---------------------------------------------------------------------------
# Database errors fail closed
# ---------------------------------------------------------------------------

def test_password_state_read_error_fails_closed(monkeypatch):
    """An outage must never reopen the "create the first password" step."""
    def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_system_setting", boom)
    monkeypatch.setattr(database, "get_system_setting_cached", boom)
    assert owner_auth.password_set() is True
    assert owner_auth.password_set(exact=True) is True
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    assert owner_auth.bootstrap_access(_request()) is False


def test_trusted_lan_read_error_reads_as_off(monkeypatch):
    def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_system_setting_cached", boom)
    assert owner_auth.trusted_lan() is False


def test_require_key_is_503_when_storage_is_locked(monkeypatch):
    from app.core import encryption
    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: False)
    with pytest.raises(HTTPException) as info:
        owner_auth.require_key()
    assert info.value.status_code == 503


# ---------------------------------------------------------------------------
# direct_local / local_access: only a real direct LAN request qualifies
# ---------------------------------------------------------------------------

@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(settings, "shuffle_local_hostnames", "workstation.lan")
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)


def test_direct_lan_request_is_local(lan):
    assert owner_auth.direct_local(_request())
    assert owner_auth.direct_local(_request(host="workstation.lan:8000"))
    assert owner_auth.direct_local(_request(host="box.tail1234.ts.net", peer="100.100.1.2"))
    assert owner_auth.local_access(_request())


@pytest.mark.parametrize("kwargs", [
    {"ingress": "remote"},
    {"ingress": None},
    {"headers": {"authorization": "Bearer anything"}},
    {"headers": {"x-forwarded-for": "8.8.8.8"}},
    {"headers": {"cf-connecting-ip": "8.8.8.8"}},
    {"peer": "8.8.8.8"},
    {"peer": "not-an-ip"},
    {"host": "attacker.example:8000"},                 # DNS rebinding
    {"host": "8.8.8.8"},
    {"host": None},
    {"host": ":8000"},
    {"headers": {"sec-fetch-site": "cross-site"}},
    {"headers": {"origin": "http://attacker.example"}},
    {"client": False},
])
def test_requests_that_only_look_local_are_refused(lan, kwargs):
    request = _request(**kwargs)
    assert owner_auth.direct_local(request) is False
    assert owner_auth.local_access(request) is False


def test_local_access_needs_the_setting(lan, monkeypatch):
    monkeypatch.setattr(settings, "shuffle_local_access", False)
    assert owner_auth.local_access(_request()) is False
    assert owner_auth.bootstrap_access(_request()) is False


def test_lan_without_trust_is_not_the_owner(lan, monkeypatch):
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    assert owner_auth.local_access(_request()) is False


# ---------------------------------------------------------------------------
# loopback / same_origin
# ---------------------------------------------------------------------------

@pytest.fixture
def no_desktop_token():
    desktop_listener.configure(None)
    yield
    desktop_listener.configure(None)


def test_loopback_refuses_proxy_headers_and_non_loopback(no_desktop_token):
    assert owner_auth.loopback_request(_request(host="localhost:8000", peer="127.0.0.1"))
    assert not owner_auth.loopback_request(_request(host="localhost:8000", peer="127.0.0.1",
                                                    headers={"x-forwarded-host": "x"}))
    assert not owner_auth.loopback_request(_request(host="localhost:8000", peer="192.168.1.2"))
    assert not owner_auth.loopback_request(_request(host="evil.example", peer="127.0.0.1"))
    assert not owner_auth.loopback_request(_request(host="localhost:8000", peer="127.0.0.1", ingress="remote"))


def test_loopback_needs_the_desktop_listener_token_when_configured():
    token = "a" * 64
    desktop_listener.configure(token)
    try:
        bare = _request(host="localhost:8000", peer="127.0.0.1")
        assert not owner_auth.loopback_request(bare)
        signed = _request(host="localhost:8000", peer="127.0.0.1", headers={desktop_listener.HEADER: token})
        assert owner_auth.loopback_request(signed)
        # The desktop's own process may omit Origin; nothing else may.
        owner_auth.same_origin(signed)
        with pytest.raises(HTTPException):
            owner_auth.same_origin(bare)
    finally:
        desktop_listener.configure(None)


@pytest.mark.parametrize("origin,scheme", [
    (None, "http"),
    ("http://attacker.example", "http"),
    ("http://192.168.1.10:8000/path", "http"),
    ("http://192.168.1.10:8000?x=1", "http"),
    ("http://u:p@192.168.1.10:8000", "http"),
    ("ftp://192.168.1.10:8000", "http"),
    ("http://192.168.1.10:8000", "https"),   # a TLS page cannot be driven from plain HTTP
])
def test_same_origin_refusals(no_desktop_token, origin, scheme):
    headers = {"origin": origin} if origin else {}
    with pytest.raises(HTTPException) as info:
        owner_auth.same_origin(_request(scheme=scheme, headers=headers))
    assert info.value.status_code == 403


def test_same_origin_accepts_matching_origin(no_desktop_token):
    owner_auth.same_origin(_request(headers={"origin": "http://192.168.1.10:8000"}))
    owner_auth.same_origin(_request(scheme="https", headers={"origin": "https://192.168.1.10:8000"}))


def test_owner_session_never_counts_on_the_remote_listener(monkeypatch):
    monkeypatch.setattr(database, "get_auth_session", lambda token: {"token": token})
    assert owner_auth.owner_session(_request(), "shr_x") is True
    assert owner_auth.owner_session(_request(ingress="remote"), "shr_x") is False


# ---------------------------------------------------------------------------
# Account browser origin and claim URLs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin", [
    "http://connect.example.com", "https://", "https://u@connect.example.com",
    "https://connect.example.com/path", "https://connect.example.com:8443",
])
def test_browser_origin_misconfiguration_is_503(monkeypatch, origin):
    monkeypatch.setattr(settings, "account_browser_origin", origin)
    with pytest.raises(HTTPException) as info:
        owner_auth.browser_origin()
    assert info.value.status_code == 503


def test_claim_url_validates_code_and_screen(monkeypatch):
    monkeypatch.setattr(settings, "account_browser_origin", "https://connect.example.com/")
    assert owner_auth.claim_url("ABCDEFGH2345") == "https://connect.example.com/browser/claim?code=ABCDEFGH2345"
    assert owner_auth.claim_url("ABCDEFGH2345", "signup").endswith("&screen=signup")
    for bad in ("abcdefgh2345", "ABCDEFGH234", "ABCDEFGH2345&x=1", None, "ABCDEFGH0189"):
        assert owner_auth.claim_url(bad) is None
    with pytest.raises(ValueError):
        owner_auth.claim_url("ABCDEFGH2345", "admin")


# ---------------------------------------------------------------------------
# OWNER_PASSWORD seeding
# ---------------------------------------------------------------------------

@pytest.fixture
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(owner_auth, "hash_password", lambda pw: _cheap_hash(pw))


def test_seed_absent(mock_db):
    assert owner_auth.seed_password_from_env({}) == "absent"


def test_seed_invalid_value_is_not_used_and_is_removed(mock_db, cheap_hashes):
    env = {owner_auth.PASSWORD_ENV: "short"}
    assert owner_auth.seed_password_from_env(env) == "invalid"
    assert owner_auth.PASSWORD_ENV not in env
    assert not database.get_system_setting(owner_auth.PASSWORD_KEY)


def test_seed_once_then_ignored_forever(mock_db, cheap_hashes):
    env = {owner_auth.PASSWORD_ENV: "first-password-123"}
    assert owner_auth.seed_password_from_env(env) == "seeded"
    assert owner_auth.PASSWORD_ENV not in env
    assert owner_auth.verify_password("first-password-123")
    assert owner_auth.seed_password_from_env({owner_auth.PASSWORD_ENV: "second-password-123"}) == "ignored"
    assert owner_auth.verify_password("first-password-123")
    assert not owner_auth.verify_password("second-password-123")


def test_seed_ignored_when_a_password_already_exists(mock_db, cheap_hashes):
    owner_auth.set_password("wizard-password-1", source="wizard")
    assert owner_auth.seed_password_from_env({owner_auth.PASSWORD_ENV: "env-password-123"}) == "ignored"
    assert database.get_system_setting(owner_auth.ENV_SEEDED_KEY) == "true"
    assert owner_auth.verify_password("wizard-password-1")


def test_seed_removes_the_variable_from_the_process_environment(mock_db, cheap_hashes, monkeypatch):
    monkeypatch.setenv(owner_auth.PASSWORD_ENV, "process-password-1")
    assert owner_auth.seed_password_from_env() == "seeded"
    assert owner_auth.PASSWORD_ENV not in os.environ


def test_set_password_signs_every_owner_browser_out(mock_db, cheap_hashes):
    token, _ = owner_auth.issue_session()
    assert owner_auth.local_session(token) is not None
    owner_auth.set_password("brand-new-password", source="settings")
    assert owner_auth.local_session(token) is None
