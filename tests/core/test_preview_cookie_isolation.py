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


def test_unreadable_cookie_key_fails_closed(monkeypatch):
    def broken_key():
        raise OSError("unreadable fixture key")

    monkeypatch.setattr(preview_access, "_key", broken_key)
    assert preview_access.cookie_token({"ais_preview_unknown": "fixture"}) is None
