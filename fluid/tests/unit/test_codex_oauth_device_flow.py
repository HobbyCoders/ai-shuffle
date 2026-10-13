"""Codex device-code OAuth: start, every poll outcome, token storage, refresh
failures, and that no token ever reaches a log record.

All HTTP goes through ``httpx.MockTransport``; nothing touches the network."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fluid.providers.openai_responses import codex_oauth as ox
from fluid.providers.openai_responses.codex_oauth import (
    CodexAuth,
    CodexAuthError,
    CodexAuthPending,
    StoredBundle,
    load_bundle,
    save_bundle,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")

ACCESS = "at-SECRET-access-0123456789"
REFRESH = "rt-SECRET-refresh-0123456789"
SECRETS = (ACCESS, REFRESH)


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64url({'alg': 'none'})}.{_b64url(claims)}.sig"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _Recorder:
    """A MockTransport handler that answers from a script and records requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


# ─── start ────────────────────────────────────────────────────────────────────


async def test_start_device_code_posts_the_client_id_and_parses_the_reply(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_APP_SERVER_LOGIN_CLIENT_ID", "app_override")
    rec = _Recorder(httpx.Response(200, json={
        "device_auth_id": "dev-1", "user_code": "ABCD-EFGH", "interval": "7",
        "expires_in": 600, "verification_uri": "https://example.test/device",
    }))
    async with _client(rec) as http:
        start = await ox.start_device_code(client=http)

    assert str(rec.requests[0].url) == ox.DEVICE_USERCODE_URL
    assert rec.body() == {"client_id": "app_override"}
    assert (start.device_code, start.user_code) == ("dev-1", "ABCD-EFGH")
    assert start.interval == 7 and start.expires_in == 600
    assert start.verification_uri == "https://example.test/device"
    assert len(start.code_verifier) >= 43


async def test_start_defaults_and_expires_at_parsing() -> None:
    later = (datetime.now(UTC) + timedelta(seconds=300)).replace(tzinfo=None).isoformat()
    rec = _Recorder(httpx.Response(200, json={
        "device_code": "dev-2", "user_code": "U", "expires_at": later,
    }))
    async with _client(rec) as http:
        start = await ox.start_device_code(client=http)

    assert start.device_code == "dev-2"
    assert start.verification_uri == ox.DEVICE_VERIFICATION_URL
    assert start.interval == 5
    assert 290 <= start.expires_in <= 300  # naive expires_at is read as UTC


async def test_start_with_past_or_unparseable_expiry() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    for expires_at, expected in ((past, 0), ("not a date", 900)):
        rec = _Recorder(httpx.Response(200, json={
            "device_auth_id": "d", "user_code": "u", "expires_at": expires_at,
        }))
        async with _client(rec) as http:
            start = await ox.start_device_code(client=http)
        assert start.expires_in == expected


@pytest.mark.parametrize("response, code", [
    (httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow"}}), "rate_limited"),
    (httpx.Response(503, text="<html>down</html>"), "http_503"),
    (httpx.Response(200, json={"user_code": "only"}), "invalid_usercode_response"),
    (httpx.Response(200, json=["not", "an", "object"]), "invalid_usercode_response"),
])
async def test_start_errors_are_typed(response, code) -> None:
    async with _client(_Recorder(response)) as http:
        with pytest.raises(CodexAuthError) as info:
            await ox.start_device_code(client=http)
    assert info.value.code == code
    assert info.value.is_permanent is False


# ─── poll ─────────────────────────────────────────────────────────────────────


async def _poll(*responses: httpx.Response) -> tuple[_Recorder, object]:
    rec = _Recorder(*responses)
    async with _client(rec) as http:
        try:
            result = await ox.poll_device_code("dev-1", "local-verifier", "ABCD", client=http)
        except (CodexAuthPending, CodexAuthError) as exc:
            result = exc
    return rec, result


async def test_poll_pending_and_slow_down() -> None:
    _, pending = await _poll(httpx.Response(400, json={"error": "authorization_pending"}))
    assert isinstance(pending, CodexAuthPending) and pending.slow_down is False
    assert str(pending) == "authorization_pending"

    _, slow = await _poll(httpx.Response(400, json={"error": {"code": "deviceauth_slow_down"}}))
    assert isinstance(slow, CodexAuthPending) and slow.slow_down is True
    assert str(slow) == "slow_down"


@pytest.mark.parametrize("error, code", [
    ({"error": "expired_token", "error_description": "code expired"}, "expired_token"),
    ({"error": {"code": "access_denied", "message": "user said no"}}, "access_denied"),
    ({"error": {"type": "invalid_request_error"}}, "invalid_request_error"),
])
async def test_poll_terminal_errors(error, code) -> None:
    _, result = await _poll(httpx.Response(400, json=error))
    assert isinstance(result, CodexAuthError) and not isinstance(result, CodexAuthPending)
    assert result.code == code


async def test_poll_http_error_without_a_body_is_terminal() -> None:
    _, result = await _poll(httpx.Response(500, text="oops"))
    assert isinstance(result, CodexAuthError) and result.code == "http_500"


async def test_poll_success_with_direct_tokens() -> None:
    id_token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-ns"}})
    rec, bundle = await _poll(httpx.Response(200, json={
        "access_token": ACCESS, "refresh_token": REFRESH, "id_token": id_token,
    }))
    assert rec.body() == {"client_id": ox.client_id(), "device_auth_id": "dev-1", "user_code": "ABCD"}
    assert bundle.access_token == ACCESS and bundle.refresh_token == REFRESH
    assert bundle.account_id == "acct-ns"


async def test_poll_success_exchanges_the_code_with_the_server_verifier() -> None:
    rec, bundle = await _poll(
        httpx.Response(200, json={"authorization_code": "code-1", "code_verifier": "server-verifier"}),
        httpx.Response(200, json={
            "access_token": ACCESS, "refresh_token": REFRESH,
            "id_token": _jwt({"chatgpt_account_id": "acct-top"}),
        }),
    )
    exchange = rec.requests[1]
    assert str(exchange.url) == ox.TOKEN_URL
    form = dict(httpx.QueryParams(exchange.content.decode()))
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-1"
    assert form["code_verifier"] == "server-verifier"
    assert form["redirect_uri"] == ox.DEVICE_CALLBACK_URL
    assert bundle.account_id == "acct-top"


async def test_poll_success_falls_back_to_the_local_verifier() -> None:
    rec, _bundle = await _poll(
        httpx.Response(200, json={"code": "code-2"}),
        httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH}),
    )
    form = dict(httpx.QueryParams(rec.requests[1].content.decode()))
    assert form["code_verifier"] == "local-verifier"


async def test_poll_success_with_neither_tokens_nor_code() -> None:
    _, result = await _poll(httpx.Response(200, json={"status": "ok"}))
    assert isinstance(result, CodexAuthError) and result.code == "invalid_poll_response"


@pytest.mark.parametrize("body, code", [
    ({"refresh_token": REFRESH}, "invalid_poll_response"),  # no access token: not a token reply
    ({"access_token": ACCESS}, "invalid_token_response"),
])
async def test_poll_success_with_an_incomplete_token_response(body, code) -> None:
    _, result = await _poll(httpx.Response(200, json=body))
    assert isinstance(result, CodexAuthError) and result.code == code


async def test_code_exchange_failure_is_typed() -> None:
    _, result = await _poll(
        httpx.Response(200, json={"code": "code-3"}),
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "used"}),
    )
    assert isinstance(result, CodexAuthError) and result.code == "invalid_grant"
    assert str(result) == "used"


# ─── JWT claims ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [
    None, "", "no-dots", "a.!!!notbase64!!!.c", f"a.{_b64url(['list'])}.c",
    f"a.{base64.urlsafe_b64encode(b'not json').decode()}.c",
])
def test_malformed_id_tokens_yield_no_account(token) -> None:
    assert ox.extract_account_id(token) is None


def test_access_token_exp_edge_cases() -> None:
    assert ox._access_token_exp(None) is None
    assert ox._access_token_exp(_jwt({"exp": "soon"})) is None
    assert ox._access_token_exp(_jwt({"exp": 10**20})) is None  # out of range
    assert ox._access_token_exp(_jwt({"exp": 0})) == datetime.fromtimestamp(0, tz=UTC)


# ─── token storage ────────────────────────────────────────────────────────────


@POSIX_ONLY
def test_saved_token_file_and_its_directory_are_owner_only(tmp_path) -> None:
    path = tmp_path / "cfg" / "fluid" / "codex_auth.json"
    old_umask = os.umask(0)
    try:
        save_bundle(StoredBundle(access_token=ACCESS, refresh_token=REFRESH), path)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@POSIX_ONLY
def test_token_bytes_are_never_readable_by_others_even_over_a_stale_temp(
    tmp_path, monkeypatch
) -> None:
    """Regression: ``os.open``'s mode only applies on creation, so a leftover
    ``*.tmp`` with 0644 held the fresh token world-readable until the rename."""
    path = tmp_path / "codex_auth.json"
    stale = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    stale.write_text("leftover")
    os.chmod(stale, 0o644)
    seen: list[int] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(ox.os, "replace", spy_replace)
    save_bundle(StoredBundle(access_token=ACCESS, refresh_token=REFRESH), path)

    assert seen == [0o600]
    assert load_bundle(path).access_token == ACCESS


def test_failed_save_removes_its_temp_and_keeps_the_old_bundle(tmp_path, monkeypatch) -> None:
    path = tmp_path / "codex_auth.json"
    save_bundle(StoredBundle(access_token="old", refresh_token="old-rt"), path)

    def boom(_fd):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(ox.os, "fsync", boom)
    with pytest.raises(OSError, match="I/O error"):
        save_bundle(StoredBundle(access_token=ACCESS, refresh_token=REFRESH), path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["codex_auth.json"]
    assert load_bundle(path).access_token == "old"


@pytest.mark.parametrize("content", [
    "{not json", "[1, 2]", '{"access_token": ""}', '{"refresh_token": "rt"}',
    # Regression: a bundle missing a required field raised TypeError out of
    # load_bundle (and so out of get_access_token/account_id).
    '{"access_token": "at"}',
])
def test_unusable_token_files_load_as_absent(tmp_path, content) -> None:
    path = tmp_path / "codex_auth.json"
    path.write_text(content)
    assert load_bundle(path) is None


async def test_bundle_missing_refresh_token_reads_as_not_authenticated(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    path.write_text('{"access_token": "at"}')
    auth = CodexAuth(token_path=path, client=_client(_Recorder()))

    assert auth.account_id is None
    with pytest.raises(CodexAuthError) as info:
        await auth.get_access_token()
    assert info.value.code == "not_authenticated"


def test_parse_iso_tolerates_garbage() -> None:
    assert ox._parse_iso(None) is None
    assert ox._parse_iso("yesterday") is None
    assert ox._parse_iso("2026-01-01T00:00:00").tzinfo is UTC


def test_windows_acl_restriction_is_best_effort(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        raise OSError("icacls missing")

    monkeypatch.setattr(ox.subprocess, "run", fake_run)
    monkeypatch.delenv("USERNAME", raising=False)
    ox._restrict_to_owner_windows(tmp_path / "f")
    assert calls == []  # no user, nothing to grant
    monkeypatch.setenv("USERNAME", "alice")
    ox._restrict_to_owner_windows(tmp_path / "f")  # the OSError is swallowed
    assert calls and calls[0][0] == "icacls" and "alice:F" in calls[0]


# ─── refresh failure paths ────────────────────────────────────────────────────


def _due_bundle(path) -> None:
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    save_bundle(StoredBundle(access_token=ACCESS, refresh_token=REFRESH,
                             account_id="acct-1", last_refresh=old), path)


@pytest.mark.parametrize("response, code, permanent", [
    (httpx.Response(400, json={"error": "refresh_token_expired"}), "refresh_token_expired", True),
    (httpx.Response(401, json={"error": {"code": "refresh_token_invalidated"}}),
     "refresh_token_invalidated", True),
    (httpx.Response(502, text="bad gateway"), "http_502", False),
    (httpx.Response(200, json={"refresh_token": "rt-2"}), "invalid_token_response", False),
])
async def test_failed_refresh_keeps_the_stored_bundle_and_releases_the_lock(
    tmp_path, response, code, permanent
) -> None:
    path = tmp_path / "codex_auth.json"
    _due_bundle(path)
    before = path.read_bytes()
    rec = _Recorder(response, httpx.Response(200, json={
        "access_token": "at-2", "refresh_token": "rt-2",
    }))
    async with _client(rec) as http:
        auth = CodexAuth(token_path=path, client=http)
        with pytest.raises(CodexAuthError) as info:
            await auth.get_access_token()
        assert info.value.code == code and info.value.is_permanent is permanent
        assert path.read_bytes() == before
        assert rec.body()["refresh_token"] == REFRESH

        # Neither the asyncio lock nor the file lock is left held.
        assert await auth.get_access_token() == "at-2"
    assert load_bundle(path).refresh_token == "rt-2"
    assert load_bundle(path).account_id == "acct-1"  # no id_token: account kept


async def test_on_401_without_any_stored_bundle_is_not_authenticated(tmp_path) -> None:
    rec = _Recorder()
    async with _client(rec) as http:
        auth = CodexAuth(token_path=tmp_path / "missing.json", client=http)
        with pytest.raises(CodexAuthError) as info:
            await auth.on_401()
    assert info.value.code == "not_authenticated"
    assert rec.requests == []


async def test_on_401_refresh_failure_leaves_the_file_alone(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    save_bundle(StoredBundle(access_token=ACCESS, refresh_token=REFRESH,
                             last_refresh=datetime.now(UTC).isoformat()), path)
    before = path.read_bytes()
    rec = _Recorder(httpx.Response(400, json={"error": "refresh_token_reused"}))
    async with _client(rec) as http:
        auth = CodexAuth(token_path=path, client=http)
        with pytest.raises(CodexAuthError) as info:
            await auth.on_401()
    assert info.value.is_permanent
    assert path.read_bytes() == before


# ─── never log a token ────────────────────────────────────────────────────────


async def test_no_flow_logs_a_token(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    id_token = _jwt({"chatgpt_account_id": "acct"})
    path = tmp_path / "codex_auth.json"
    rec = _Recorder(
        httpx.Response(200, json={"device_auth_id": "dev", "user_code": "U"}),
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"code": "c", "code_verifier": "v"}),
        httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH,
                                  "id_token": id_token}),
        httpx.Response(200, json={"access_token": ACCESS + "-2", "refresh_token": REFRESH + "-2"}),
        httpx.Response(400, json={"error": "refresh_token_reused",
                                  "error_description": "reused"}),
    )
    async with _client(rec) as http:
        start = await ox.start_device_code(client=http)
        with pytest.raises(CodexAuthPending):
            await ox.poll_device_code(start.device_code, start.code_verifier, start.user_code, client=http)
        bundle = await ox.poll_device_code(start.device_code, start.code_verifier, start.user_code,
                                           client=http)
        save_bundle(StoredBundle.from_bundle(bundle, last_refresh="2000-01-01T00:00:00+00:00"), path)
        auth = CodexAuth(token_path=path, client=http)
        await auth.get_access_token()
        with pytest.raises(CodexAuthError):
            await auth.on_401()
    # Malformed tokens go through the debug-logged decode path too.
    ox.extract_account_id(f"x.{ACCESS}.y")

    text = caplog.text + "".join(str(r.args) for r in caplog.records)
    for secret in (*SECRETS, ACCESS + "-2", REFRESH + "-2"):
        assert secret not in text
