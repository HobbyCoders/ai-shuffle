"""Codex OAuth: token-file round-trip, rotating refresh, proactive window, 401.

All offline — the token endpoint is an ``httpx.MockTransport``; no network."""

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omni.providers.openai_responses import codex_oauth as ox
from omni.providers.openai_responses.codex_oauth import (
    CodexAuth,
    StoredBundle,
    _needs_refresh,
    generate_pkce,
    load_bundle,
    save_bundle,
)


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64url({'alg': 'none'})}.{_b64url(claims)}.sig"


def _far_future_access() -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    return _jwt({"exp": exp})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── PKCE ─────────────────────────────────────────────────────────────────────


def test_pkce_challenge_is_s256_of_verifier() -> None:
    import hashlib

    verifier, challenge = generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


# ─── token-file round-trip ───────────────────────────────────────────────────


def test_token_file_round_trip_and_perms(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    bundle = StoredBundle(
        access_token="at",
        refresh_token="rt",
        id_token="it",
        account_id="acct_1",
        last_refresh=_now_iso(),
    )
    save_bundle(bundle, path)
    assert (os.stat(path).st_mode & 0o777) == 0o600
    loaded = load_bundle(path)
    assert loaded == bundle


def test_load_bundle_missing_returns_none(tmp_path) -> None:
    assert load_bundle(tmp_path / "nope.json") is None


def test_load_bundle_ignores_unknown_keys(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "extra": 1}))
    loaded = load_bundle(path)
    assert loaded is not None
    assert loaded.access_token == "a"


# ─── proactive-refresh window ────────────────────────────────────────────────


def test_needs_refresh_proactive_8_day_interval() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    bundle = StoredBundle(
        access_token=_far_future_access(), refresh_token="rt", last_refresh=old
    )
    assert _needs_refresh(bundle) is True


def test_needs_refresh_false_when_fresh() -> None:
    bundle = StoredBundle(
        access_token=_far_future_access(), refresh_token="rt", last_refresh=_now_iso()
    )
    assert _needs_refresh(bundle) is False


def test_needs_refresh_true_when_exp_within_skew() -> None:
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
    bundle = StoredBundle(
        access_token=_jwt({"exp": exp}), refresh_token="rt", last_refresh=_now_iso()
    )
    assert _needs_refresh(bundle) is True


def test_needs_refresh_true_when_never_refreshed() -> None:
    bundle = StoredBundle(access_token="x", refresh_token="rt", last_refresh=None)
    assert _needs_refresh(bundle) is True


# ─── rotating refresh via CodexAuth ──────────────────────────────────────────


def _token_endpoint(calls: list, *, rotate_to: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ox.TOKEN_URL
        body = json.loads(request.content)
        assert body["grant_type"] == "refresh_token"
        calls.append(body["refresh_token"])
        return httpx.Response(
            200,
            json={
                "access_token": _far_future_access(),
                "refresh_token": rotate_to,
                "id_token": _jwt({"chatgpt_account_id": "acct_new"}),
            },
        )

    return handler


async def test_get_access_token_refreshes_and_persists_rotation(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    save_bundle(
        StoredBundle(
            access_token="at_old",
            refresh_token="rt_old",
            account_id="acct_old",
            last_refresh=old,
        ),
        path,
    )
    calls: list = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(_token_endpoint(calls, rotate_to="rt_new")))
    auth = CodexAuth(token_path=path, client=http)

    token = await auth.get_access_token()
    assert token == (await auth.get_access_token())  # second call is a no-op
    await http.aclose()

    # Exactly one refresh POST, sent with the OLD refresh token.
    assert calls == ["rt_old"]
    # Rotation persisted; account id updated from the fresh id_token.
    on_disk = load_bundle(path)
    assert on_disk.refresh_token == "rt_new"
    assert on_disk.account_id == "acct_new"
    assert auth.account_id == "acct_new"


async def test_get_access_token_raises_without_stored_bundle(tmp_path) -> None:
    auth = CodexAuth(token_path=tmp_path / "absent.json")
    with pytest.raises(ox.CodexAuthError):
        await auth.get_access_token()


# ─── on_401: refresh once, skip when a peer already rotated ──────────────────


async def test_on_401_refreshes_once(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    save_bundle(
        StoredBundle(
            access_token=_far_future_access(),  # not proactively due
            refresh_token="rt_old",
            last_refresh=_now_iso(),
        ),
        path,
    )
    calls: list = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(_token_endpoint(calls, rotate_to="rt_new")))
    auth = CodexAuth(token_path=path, client=http)

    await auth.get_access_token()  # loads bundle; no refresh (fresh)
    assert calls == []
    await auth.on_401()  # forces exactly one refresh
    await http.aclose()

    assert calls == ["rt_old"]
    assert load_bundle(path).refresh_token == "rt_new"


async def test_on_401_skips_post_when_peer_already_rotated(tmp_path) -> None:
    path = tmp_path / "codex_auth.json"
    save_bundle(
        StoredBundle(
            access_token="at_stale", refresh_token="rt_old", last_refresh=_now_iso()
        ),
        path,
    )
    calls: list = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(_token_endpoint(calls, rotate_to="rt_new")))
    auth = CodexAuth(token_path=path, client=http)
    await auth.get_access_token()  # in-memory access = "at_stale"

    # A peer refreshed and wrote a new bundle to disk while we held the stale one.
    save_bundle(
        StoredBundle(
            access_token="at_peer", refresh_token="rt_peer", last_refresh=_now_iso()
        ),
        path,
    )
    await auth.on_401()
    await http.aclose()

    # No POST — the disk token differs from ours, so we adopt it instead.
    assert calls == []
    assert auth._bundle.access_token == "at_peer"


# ─── refresh error mapping ───────────────────────────────────────────────────


async def test_refresh_permanent_failure_is_flagged(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "refresh_token_reused"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ox.CodexAuthError) as ei:
            await ox.refresh_tokens("rt", client=http)
        assert ei.value.is_permanent is True
    finally:
        await http.aclose()


def test_build_authorize_url_has_pkce_challenge() -> None:
    start = ox.build_authorize_url("http://localhost:1455/auth/callback")
    assert start.url.startswith(ox.AUTHORIZE_URL + "?")
    assert "code_challenge=" in start.url
    assert "code_challenge_method=S256" in start.url
    assert start.redirect_uri == "http://localhost:1455/auth/callback"
