"""A browser cookie jar is host-scoped, not port-scoped (#170)."""
import httpx
import pytest
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient

from app import preview
from app.api import auth, terminals, ws_auth
from app.core import preview_access
from app.core.config import settings
from app.serve import IngressApplication


@pytest.mark.parametrize("reverse", [False, True])
def test_two_instances_share_a_browser_without_overwriting_grants(tmp_path, monkeypatch, reverse):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(settings, "instance_id", "cookie-isolation-test")
    api = FastAPI()
    api.include_router(auth.router)

    @api.get("/read")
    def read(_: str = Depends(auth.require_auth)):
        return {"ok": True}

    @api.get("/branch")
    def branch(request: Request):
        preview._require_preview_owner(request)
        return {"ok": True}

    @api.post("/api/v1/settings/probe")
    def mutation(_: str = Depends(auth.require_auth)):
        pytest.fail("Preview mutation ceiling was bypassed")

    @api.websocket("/ws")
    async def ws(socket: WebSocket):
        authenticated, _ = await ws_auth.authenticate_websocket(socket, None)
        await socket.accept()
        await socket.send_json({
            "authenticated": authenticated,
            "terminal_restricted": terminals._preview_cookie_present(socket),
        })
        await socket.close()

    instances = [(tmp_path / "one", 18102), (tmp_path / "two", 18202)]
    if reverse:
        instances.reverse()
    jar = httpx.Cookies()
    names = []
    for data_dir, port in instances:
        monkeypatch.setattr(settings, "data_dir", data_dir)
        client = TestClient(IngressApplication(api, port - 2, port - 1, port),
                            base_url=f"http://127.0.0.1:{port}", client=("127.0.0.1", 9000))
        client.cookies.update(jar)
        token = preview_access.mint("session-" + data_dir.name)
        response = client.get(preview_access.REDIRECT_PATH, params={"token": token}, follow_redirects=False)
        assert response.status_code == 303
        names.append(next(iter(response.cookies)))
        jar.update(client.cookies)

    # Read both AGAIN, after both redirect responses entered the same jar.
    for data_dir, port in instances:
        monkeypatch.setattr(settings, "data_dir", data_dir)
        client = TestClient(IngressApplication(api, port - 2, port - 1, port),
                            base_url=f"http://127.0.0.1:{port}", client=("127.0.0.1", 9000))
        client.cookies.update(jar)
        assert client.get("/read").status_code == 200
        assert client.get("/branch").status_code == 200
        origin = {"Origin": f"http://127.0.0.1:{port}"}
        assert client.post("/api/v1/settings/probe", headers=origin).status_code == 403
        with client.websocket_connect(f"ws://127.0.0.1:{port}/ws", headers=origin) as socket:
            assert socket.receive_json() == {"authenticated": True, "terminal_restricted": True}
        # A valid cookie remains useless on the protected remote listener.
        assert client.get(f"http://127.0.0.1:{port - 1}/read").status_code == 401
    assert len(set(names)) == 2


def test_cookie_namespace_is_stable_and_does_not_accept_another_instances_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "one")
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    first = preview_access.cookie_name()
    token = preview_access.mint("one")
    # A second read uses the persisted key, not a new per-process random name.
    assert preview_access.cookie_name() == first
    monkeypatch.setattr(settings, "data_dir", tmp_path / "two")
    second = preview_access.cookie_name()
    assert first != second
    monkeypatch.setattr(settings, "data_dir", tmp_path / "one")
    assert preview_access.cookie_token({second: token}) is None
    assert preview_access.cookie_token({first: token, second: "foreign"}) == token


@pytest.mark.parametrize("cookies", [{}, {"session": "owner"}, {"theme": "dark", "session": "owner"}])
def test_ordinary_cookies_never_read_or_create_preview_key(tmp_path, monkeypatch, cookies):
    from unittest.mock import Mock

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    key = Mock(wraps=preview_access._key)
    monkeypatch.setattr(preview_access, "_key", key)
    assert preview_access.cookie_token(cookies) is None
    key.assert_not_called()
    assert not preview_access._key_path().exists()


async def test_owner_websocket_without_preview_does_not_read_key(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.core import owner_auth
    from tests.api.test_ws_auth_paths import _socket

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(owner_auth, "local_access", lambda conn: False)
    monkeypatch.setattr(owner_auth, "owner_session", lambda conn, token: token == "owner")
    key = Mock(wraps=preview_access._key)
    monkeypatch.setattr(preview_access, "_key", key)
    assert await ws_auth.authenticate_websocket(_socket(cookies={"session": "owner"}), None) == (True, None)
    key.assert_not_called()


def test_preview_ingress_with_only_owner_cookie_does_not_read_key(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from tests.core.test_preview_listener import _client, PREVIEW

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    key = Mock(wraps=preview_access._key)
    monkeypatch.setattr(preview_access, "_key", key)
    assert _client(PREVIEW, cookies={"session": "owner"}).get("/").status_code == 403
    key.assert_not_called()


def test_cookie_name_is_cached_per_key_path(tmp_path, monkeypatch):
    from unittest.mock import Mock

    key = Mock(wraps=preview_access._key)
    monkeypatch.setattr(preview_access, "_key", key)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "one")
    first = preview_access.cookie_name()
    for _ in range(5):
        assert preview_access.cookie_name() == first
        assert preview_access.cookie_token({first: "fixture"}) == "fixture"
    assert key.call_count == 1
    monkeypatch.setattr(settings, "data_dir", tmp_path / "two")
    second = preview_access.cookie_name()
    assert second != first
    assert key.call_count == 2
    monkeypatch.setattr(settings, "data_dir", tmp_path / "one")
    assert preview_access.cookie_name() == first
    assert key.call_count == 2


@pytest.mark.parametrize("warm_cache", [False, True])
@pytest.mark.parametrize("dependency", ["require_auth", "require_admin", "require_api_key"])
def test_unreadable_cookie_key_refuses_other_credentials(tmp_path, monkeypatch, warm_cache, dependency):
    from fastapi import HTTPException
    from tests.core.test_preview_access import _request

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    token = preview_access.mint("fixture")
    name = preview_access.cookie_name() if warm_cache else "ais_preview_unknown"

    def broken_key(*args):
        raise OSError("unreadable fixture key")

    monkeypatch.setattr(preview_access, "_key", broken_key)
    request = _request("/api/v1/settings/probe", "POST",
                       cookies={name: token, "session": "owner"},
                       headers={"Authorization": "Bearer owner", "Origin": "http://10.0.0.30:8000"})
    with pytest.raises(HTTPException) as refused:
        getattr(auth, dependency)(request)
    assert refused.value.status_code == 503
    assert preview_access.grant(request) is None
    # Terminal restrictions must also survive an unreadable key.
    assert terminals._preview_cookie_present(request) is True


def test_legacy_cookie_keeps_access_and_restrictions(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from tests.core.test_preview_access import _request

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    token = preview_access.mint("legacy")
    cookies = {"ais_preview": token}
    assert preview_access.grant(_request(cookies=cookies)).session_id == "legacy"
    assert terminals._preview_cookie_present(_request(cookies=cookies)) is True
    with pytest.raises(HTTPException) as refused:
        preview_access.enforce_limits(_request("/api/v1/settings/probe", "POST", cookies=cookies))
    assert refused.value.status_code == 403
    # An older grant cannot displace the current namespaced grant.
    current = preview_access.mint("current")
    assert preview_access.cookie_token({preview_access.cookie_name(): current, "ais_preview": token}) == current
    # The preview listener uses the same compatibility reader for HTTP and WS.
    from tests.core.test_preview_listener import _client, PREVIEW, WS

    client = _client(PREVIEW, cookies=cookies)
    assert client.get("/api/v1/whoami").json()["grant"] == "legacy"
    with client.websocket_connect(WS) as socket:
        assert socket.receive_json() == {"ingress": "preview"}
