import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api import browser_panel as panel
from app.core import browser_control as control, browser_uploads as uploads
from app.core import browser_panel_registry as registry
from tests.core.test_browser_control import state, command


@pytest.fixture
async def upload_client(monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(panel.router)
    app.dependency_overrides[panel.require_auth] = lambda: "test"
    async def authorized(request, sid):
        if sid != "upload-test":
            from fastapi import HTTPException
            raise HTTPException(403)
    monkeypatch.setattr(panel, "require_session", authorized)
    monkeypatch.setattr(uploads, "upload_root", lambda: tmp_path / "staging")
    b = control.get("upload-test")
    await b.register("runtime")
    value = state(viewer="viewer")
    value["editors"] = [dict(id="file", type="file", label="Files", value="", x=0, y=0, width=100, height=30, accept=".txt", multiple=True)]
    await b.report("runtime", value, [])
    async def send(message): pass
    b.viewers["viewer"] = send
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, b, tmp_path / "staging"
    registry.clear("upload-test")
    await asyncio.sleep(0)




@pytest.mark.asyncio
@pytest.mark.parametrize("kind, status", [("empty", 400), ("too_many", 400), ("too_big", 413), ("wrong_session", 403)])
async def test_upload_rejects_invalid_requests_before_queueing(upload_client, kind, status):
    client, broker, staging = upload_client
    files = [("files", ("x.txt", b"data"))] * (11 if kind == "too_many" else 1)
    response = await client.post(
        URL.replace("upload-test", "other-session") if kind == "wrong_session" else URL,
        params=PARAMS, files=None if kind == "empty" else files,
        headers={"content-length": str(uploads.MAX_BODY + 1)} if kind == "too_big" else {},
    )
    assert response.status_code == status
    assert not broker.pending and not staging.exists()


@pytest.mark.asyncio
async def test_target_is_rechecked_after_receiving_file_bytes(upload_client):
    client, broker, staging = upload_client
    async def chunks():
        yield b'--b\r\nContent-Disposition: form-data; name="files"; filename="x.txt"\r\n\r\n'
        broker.state["active_tab_id"] = "changed"
        yield b'data\r\n--b--\r\n'
    response = await client.post(URL, params=PARAMS, content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert response.status_code == 409 and not broker.pending
    assert not staging.exists()


URL = "/api/v1/sessions/upload-test/browser/panel/upload"
PARAMS = dict(viewer_id="viewer", runtime_id="runtime", tab_id="tab", field="file")


@pytest.mark.asyncio
async def test_upload_stages_original_bytes_waits_for_ack_and_cleans(upload_client):
    client, broker, staging = upload_client
    task = asyncio.create_task(client.post(URL, params=PARAMS, files=[("files", ("../document.txt", b"real bytes", "text/plain"))]))
    commands = await asyncio.wait_for(broker.poll("runtime"), 3)
    assert len(commands) == 1 and commands[0]["op"] == "upload"
    path = Path(commands[0]["args"]["paths"][0])
    assert path.name == "document.txt" and path.read_bytes() == b"real bytes"
    assert path.is_relative_to(staging)
    await broker.settle(commands[0]["id"], True)
    assert (await task).status_code == 200
    assert not list(staging.iterdir())
    assert not broker.result_waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [dict(viewer_id="other"), dict(tab_id="stale"), dict(runtime_id="stale"), dict(field="missing")])
async def test_upload_rejects_wrong_owner_or_stale_target(upload_client, change):
    client, broker, staging = upload_client
    result = await client.post(URL, params={**PARAMS, **change}, files={"files": ("x.txt", b"no")})
    assert result.status_code == 409
    assert not broker.pending and not staging.exists()


@pytest.mark.asyncio
async def test_browser_socket_cannot_supply_upload_paths(upload_client):
    _, broker, _ = upload_client
    with pytest.raises(control.ControlError, match="Unsupported operation"):
        await broker.submit("viewer", command(op="upload", args={"field": "file", "paths": ["/etc/passwd"]}))


@pytest.mark.asyncio
async def test_size_limit_cannot_be_bypassed_with_chunked_body(upload_client, monkeypatch):
    client, broker, staging = upload_client
    monkeypatch.setattr(uploads, "MAX_BODY", 100)
    async def chunks():
        yield b"--boundary\r\nContent-Disposition: form-data; name=\"files\"; filename=\"x.txt\"\r\n\r\n"
        yield b"x" * 101
        yield b"\r\n--boundary--\r\n"
    result = await client.post(URL, params=PARAMS, content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=boundary"})
    assert result.status_code == 413 and not broker.pending
    assert not staging.exists()


@pytest.mark.asyncio
async def test_failure_or_disconnect_cleans_staging(upload_client):
    client, broker, staging = upload_client
    task = asyncio.create_task(client.post(URL, params=PARAMS, files={"files": ("x.txt", b"data")}))
    await asyncio.wait_for(broker.poll("runtime"), 3)
    await broker.detach("viewer")
    assert (await task).status_code == 409
    assert not list(staging.iterdir())
    assert not broker.result_waiters
