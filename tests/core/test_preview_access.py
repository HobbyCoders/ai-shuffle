"""Session-scoped preview access for the agent browser (round-9 audit, item 4).

Nine sessions hit "only accepts direct connections from its local network" /
``local_access: false`` when the agent browser opened the worker's own UI,
and five then built their own preview servers. The token is the narrow door:
minted per session, HMAC under a per-install key, LAN-only on the local
listener, honoured from an HttpOnly cookie, and never for the routes that
change ownership, credentials or reach.
"""
from __future__ import annotations

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core import preview_access
from app.core.config import settings
from app.serve import IngressApplication

SESSION = "sess-preview-1"


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # No managed DB in unit tests → the signature alone decides.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    # The direct-LAN owner here needs "trusted LAN, no login" (off by default).
    from app.core import owner_auth
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)


def _request(path="/", method="GET", *, peer="10.0.0.200", port=8000, ingress="local",
             cookies=None, headers=None):
    raw_headers = [(b"host", f"10.0.0.135:{port}".encode())]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    if cookies:
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http", "method": method, "scheme": "http", "path": path,
        "query_string": b"", "server": ("0.0.0.0", port), "client": (peer, 50000),
        "headers": raw_headers,
    }
    if ingress:
        scope["shuffle.ingress"] = ingress
    return Request(scope)


# ─── token mechanics ─────────────────────────────────────────────────────────


def test_concurrent_first_use_publishes_one_key(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(8)
    original = preview_access.secrets.token_hex

    def fresh(size):
        value = original(size)
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(preview_access.secrets, "token_hex", fresh)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: preview_access.mint(SESSION), range(8)))
    assert all(preview_access.verify(token) is not None for token in tokens)
    assert not list(preview_access._key_path().parent.glob("preview-access-*.tmp"))


def test_invalid_existing_key_is_not_rotated():
    path = preview_access._key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid preview access key"):
        preview_access.mint(SESSION)
    assert path.read_text(encoding="utf-8") == "invalid"


def test_mint_and_verify_round_trip():
    token = preview_access.mint(SESSION)
    found = preview_access.verify(token)
    assert found is not None and found.session_id == SESSION
    assert found.expires_at > time.time()


def test_tampered_expired_and_foreign_tokens_are_refused(monkeypatch):
    token = preview_access.mint(SESSION, ttl_seconds=60)
    head, sig = token.rsplit(".", 1)
    assert preview_access.verify(head + "." + ("0" * len(sig))) is None
    assert preview_access.verify(token + "x") is None
    assert preview_access.verify(head + "." + "é" * 64) is None
    assert preview_access.verify("v1.garbage") is None
    assert preview_access.verify(token, now=time.time() + 61) is None
    # Another install's key signs differently: rotate the key file.
    preview_access._key_path().unlink()
    assert preview_access.verify(token) is None


def test_token_is_bound_to_a_live_session(monkeypatch):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr("app.db.sessions.get_session", lambda sid: {"id": sid} if sid == SESSION else None)
    assert preview_access.verify(preview_access.mint(SESSION)) is not None
    assert preview_access.verify(preview_access.mint("sess-gone")) is None


def test_preview_url_carries_the_token_and_a_safe_next():
    url = preview_access.preview_url(SESSION, "10.0.0.135", 8000, next_path="/chat/abc")
    assert url.startswith("http://10.0.0.135:8000/api/v1/auth/preview-access?token=v1.")
    assert url.endswith("&next=/chat/abc")
    assert preview_access.safe_next("//evil.example") == "/"
    assert preview_access.safe_next("https://evil.example/x") == "/"
    assert preview_access.safe_next("/settings") == "/settings"


# ─── transport and route policy ──────────────────────────────────────────────


def test_grant_requires_cookie_local_listener_and_private_peer():
    token = preview_access.mint(SESSION)
    cookie = {preview_access.COOKIE_NAME: token}
    assert preview_access.grant(_request(cookies=cookie)).session_id == SESSION
    assert preview_access.grant(_request()) is None  # no cookie
    assert preview_access.grant(_request(cookies=cookie, ingress="remote")) is None
    assert preview_access.grant(_request(cookies=cookie, ingress=None)) is None
    assert preview_access.grant(_request(cookies=cookie, peer="203.0.113.4")) is None
    # The audited failure shape — a branch's Vite dev proxy on the LAN adding
    # x-forwarded-host — is exactly what the token is for, so it passes.
    proxied = _request(cookies=cookie, headers={"X-Forwarded-Host": "localhost:5173"})
    assert preview_access.grant(proxied).session_id == SESSION
    # ... while the same header from a public peer still fails on the peer.
    assert preview_access.grant(_request(cookies=cookie, peer="203.0.113.4",
                                         headers={"X-Forwarded-Host": "localhost:5173"})) is None


@pytest.mark.parametrize("path,method,allowed", [
    ("/api/v1/sessions", "GET", True),
    ("/api/v1/sessions/abc/messages", "POST", True),
    ("/api/v1/query", "POST", True),
    ("/api/v1/auth/status", "GET", True),
    ("/api/v1/auth/logout", "POST", False),
    ("/api/v1/pairing/grant", "POST", False),
    ("/api/v1/model-providers/x", "PUT", False),
    ("/api/v1/settings/anything", "PATCH", False),
    ("/api/settings/import", "POST", False),
    ("/api/v1/network/peers", "DELETE", False),
    ("/api/v1/plugins/install", "POST", False),
])
def test_mutation_policy(path, method, allowed):
    assert preview_access.mutation_allowed(path, method) is allowed
    cookie = {preview_access.COOKIE_NAME: preview_access.mint(SESSION)}
    found = preview_access.grant(_request(path, method, cookies=cookie))
    assert (found is not None) is allowed


# ─── the redirect endpoint + require_auth, on a real ingress-wrapped app ──────


@pytest.fixture
def app():
    from app.api import auth as auth_api

    api = FastAPI()
    api.include_router(auth_api.router)

    @api.get("/api/v1/sessions")
    async def sessions(_: str = Depends(auth_api.require_auth)):
        return {"ok": True}

    @api.post("/api/v1/sessions/abc/messages")
    async def post_message(_: str = Depends(auth_api.require_auth)):
        return {"posted": True}

    @api.post("/api/v1/auth/danger")
    async def danger(_: str = Depends(auth_api.require_auth)):
        return {"changed": True}

    return IngressApplication(api, local_port=18090)


# Every request below carries the header the branch's Vite dev proxy adds —
# the exact shape that produced ``local_access: false`` in nine sessions.
_VITE = {"X-Forwarded-Host": "localhost:5173"}


def _client(app, peer="10.0.0.200", port=18090):
    return TestClient(app, base_url=f"http://10.0.0.135:{port}", client=(peer, 54321), headers=_VITE)


def test_redirect_sets_the_cookie_and_the_ui_reads_as_owner(app):
    c = _client(app)
    # Through the dev proxy the browser is NOT the local owner (audited failure).
    assert c.get("/api/v1/sessions").status_code == 401
    url = preview_access.preview_url(SESSION, "10.0.0.135", 18090, next_path="/chat/abc")
    landing = c.get(url.split("18090", 1)[1], follow_redirects=False)
    assert landing.status_code == 303
    assert landing.headers["location"] == "/chat/abc"
    set_cookie = landing.headers["set-cookie"]
    assert preview_access.COOKIE_NAME in set_cookie and "HttpOnly" in set_cookie
    # Every later request rides the cookie.
    assert c.get("/api/v1/sessions").json() == {"ok": True}
    posted = c.post("/api/v1/sessions/abc/messages", headers={"Origin": "http://10.0.0.135:18090"})
    assert posted.status_code == 200
    # ... except the routes that change ownership or credentials.
    assert c.post("/api/v1/auth/danger", headers={"Origin": "http://10.0.0.135:18090"}).status_code == 403


def test_redirect_refuses_bad_tokens_remote_ingress_and_public_peers(app):
    good = preview_access.mint(SESSION)
    assert _client(app).get(f"/api/v1/auth/preview-access?token={good}x", follow_redirects=False).status_code == 403
    assert _client(app, port=8001).get(f"/api/v1/auth/preview-access?token={good}", follow_redirects=False).status_code == 403
    assert _client(app, peer="203.0.113.4").get(f"/api/v1/auth/preview-access?token={good}", follow_redirects=False).status_code == 403
    no_ingress = TestClient(app.app, base_url="http://10.0.0.135:18090", client=("10.0.0.200", 1))
    assert no_ingress.get(f"/api/v1/auth/preview-access?token={good}", follow_redirects=False).status_code == 403


def test_cookie_is_useless_on_the_remote_listener(app):
    c = _client(app)
    c.get(f"/api/v1/auth/preview-access?token={preview_access.mint(SESSION)}", follow_redirects=False)
    assert c.get("/api/v1/sessions").status_code == 200
    assert c.get("http://10.0.0.135:8001/api/v1/sessions").status_code == 401


def test_branch_preview_routes_accept_the_cookie(monkeypatch):
    """app.preview's owner gate admits the same cookie under the same rules."""
    from fastapi import HTTPException

    from app import preview

    cookie = {preview_access.COOKIE_NAME: preview_access.mint(SESSION)}
    vite = {"X-Forwarded-Host": "localhost:5173"}
    with pytest.raises(HTTPException):
        preview._require_preview_owner(_request("/chat", headers=vite))
    preview._require_preview_owner(_request("/chat", cookies=cookie, headers=vite))
    with pytest.raises(HTTPException):
        preview._require_preview_owner(_request("/chat", cookies=cookie, ingress="remote"))
    with pytest.raises(HTTPException):
        preview._require_preview_owner(_request("/chat", cookies=cookie, peer="203.0.113.4"))


def test_transport_check_uses_the_shared_owner_auth_helpers():
    """``_private_peer`` is built on ``owner_auth``'s own address families and
    proxy-header list, so the preview door can never trust a wider network
    than the local owner does. Pinned by name so a rename there breaks here."""
    import ipaddress

    from app.core import owner_auth

    assert "x-forwarded-host" in owner_auth.PROXY_HEADERS
    assert owner_auth.is_private_address(ipaddress.ip_address("10.0.0.200"))
    assert owner_auth.is_private_address(ipaddress.ip_address("100.64.1.1"))
    assert not owner_auth.is_private_address(ipaddress.ip_address("203.0.113.4"))
    assert preview_access._private_peer(_request(peer="192.168.1.9"))
    assert not preview_access._private_peer(_request(peer="203.0.113.4"))
    assert not preview_access._private_peer(_request(ingress="remote"))

@pytest.mark.parametrize("dependency", ["require_auth", "require_admin", "require_api_key", "get_current_api_user"])
@pytest.mark.parametrize("identity", ["local-owner", "admin-cookie", "paired-cookie", "paired-bearer", "api-key"])
@pytest.mark.parametrize("transport", ["direct-LAN", "vite-proxy", "remote"])
@pytest.mark.parametrize("cookie_state", ["valid", "expired", "invalid", "missing"])
def test_preview_ceiling_precedes_every_authentication_path(monkeypatch, dependency, identity, transport, cookie_state):
    from fastapi import HTTPException
    from app.api import auth as auth_api

    paired = {"id": "paired", "account_owner_admin": True}
    monkeypatch.setattr(auth_api, "get_paired_client_from_token",
                        lambda token, **kw: paired if token == "paired-token" else None)
    monkeypatch.setattr(auth_api.auth_service, "validate_session", lambda token: token == "admin-token")
    monkeypatch.setattr(auth_api.db, "get_api_user_by_key_hash",
                        lambda hashed: {"id": "api-user"} if hashed == auth_api.hash_api_key("api-token") else None)
    monkeypatch.setattr(auth_api.db, "update_api_user_last_used", lambda ident: None)
    cookies = {}
    if cookie_state != "missing":
        cookies[preview_access.COOKIE_NAME] = ("invalid" if cookie_state == "invalid" else
            preview_access.mint(SESSION, ttl_seconds=60))
        if cookie_state == "expired":
            now = time.time()
            monkeypatch.setattr(preview_access.time, "time", lambda: now + 120)
    headers = {"Origin": "http://10.0.0.135:8000"}
    if transport == "vite-proxy":
        headers.update(_VITE)
    if identity.endswith("cookie"):
        cookies["session"] = "admin-token" if identity == "admin-cookie" else "paired-token"
    elif identity in ("paired-bearer", "api-key"):
        headers["Authorization"] = "Bearer " + ("paired-token" if identity == "paired-bearer" else "api-token")
    request = _request("/api/v1/settings/test-only", "POST", cookies=cookies, headers=headers,
                       peer="127.0.0.1" if identity == "admin-cookie" else "10.0.0.200",
                       ingress="remote" if transport == "remote" else "local")
    call = getattr(auth_api, dependency)
    if cookie_state == "valid":
        with pytest.raises(HTTPException) as exc:
            call(request)
        assert exc.value.status_code == 403
        assert "Agent previews" in exc.value.detail
        assert request.state.preview_grant.session_id == SESSION
        return

    # A broken/absent cookie does not change the normal credential's authority.
    api_only = dependency in ("require_api_key", "get_current_api_user")
    admitted = (identity == "api-key" if api_only else
                identity.startswith("paired") or
                (identity == "api-key" and dependency == "require_auth") or
                (transport == "direct-LAN" and identity != "api-key"))
    if admitted:
        assert call(request)
    else:
        with pytest.raises(HTTPException) as exc:
            call(request)
        assert "Agent previews" not in exc.value.detail
    assert request.state.preview_grant is None


@pytest.mark.parametrize("prefix", preview_access.MUTATION_DENY_PREFIXES)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("dependency", ["require_auth", "require_admin"])
def test_direct_owner_preview_cannot_reach_protected_handler(prefix, method, dependency):
    from unittest.mock import Mock
    from app.api import auth as auth_api

    api = FastAPI()
    handler = Mock(return_value={"changed": True})

    @api.api_route(prefix + "/test-only", methods=[method])
    def protected(_: str = Depends(getattr(auth_api, dependency))):
        return handler()

    client = TestClient(IngressApplication(api, local_port=18090),
                        base_url="http://10.0.0.135:18090", client=("10.0.0.200", 54321),
                        headers={"Origin": "http://10.0.0.135:18090"})
    client.cookies.set(preview_access.COOKIE_NAME, preview_access.mint(SESSION))
    assert client.request(method, prefix + "/test-only").status_code == 403
    handler.assert_not_called()


@pytest.mark.parametrize("proxied", [False, True])
def test_preview_retains_reads_and_chat_mutations_but_not_admin_authority(app, proxied):
    from fastapi import HTTPException
    from app.api.auth import require_admin

    client = _client(app)
    if not proxied:
        client.headers.pop("X-Forwarded-Host")
    client.cookies.set(preview_access.COOKIE_NAME, preview_access.mint(SESSION))
    assert client.get("/api/v1/sessions").status_code == 200
    assert client.post("/api/v1/sessions/abc/messages", headers={"Origin": "http://10.0.0.135:18090"}).status_code == 200
    request = _request("/api/v1/settings/test-only", cookies={preview_access.COOKIE_NAME: preview_access.mint(SESSION)},
                       headers=_VITE if proxied else {})
    if proxied:
        with pytest.raises(HTTPException):
            require_admin(request)
    else:
        assert require_admin(request) == "local-owner"
    assert request.state.preview_grant.session_id == SESSION
