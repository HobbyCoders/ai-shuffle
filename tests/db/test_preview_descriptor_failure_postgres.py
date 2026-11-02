"""A preview grant answers to its session row, whatever the descriptor file says.

The supervisor's ``managed-postgres.json`` is read once to open the pool; a
running worker keeps using that pool whatever later happens to the file. These
tests fail the file through the real reader while the real pool still answers,
create and delete sessions with the real ``app.db.sessions`` functions, and go
through the real listener, redirect, auth dependencies and socket check.

A failed descriptor must not read as "this process has no database": that would
skip the row check and let a deleted session, or one that runs as an API key,
keep its grant until the token expires.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth, ws_auth
from app.api.auth import require_auth
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.db import base, database, sessions
from app.db.schema_postgres import create_postgres_schema
from app.serve import IngressApplication
from tests.db import schema_lifecycle_helpers as lifecycle

LOCAL, REMOTE, PREVIEW = 8000, 8001, 8002
PROFILE = "preview-descriptor"
DESCRIPTOR = "managed-postgres.json"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A supervisor-published descriptor in a private DATA_DIR, with the schema."""
    if os.name == "nt":
        pytest.skip("the descriptor faults below rely on POSIX modes and links")
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not available")
    data: Path = tmp_path_factory.mktemp("preview-descriptor-pg")
    with lifecycle.private_postgres(bindir, data) as descriptor:
        conn = lifecycle.pg_connect(descriptor)
        try:
            conn.autocommit = False
            create_postgres_schema(conn.cursor())
            conn.commit()
        finally:
            conn.close()
        yield data


@pytest.fixture
def worker(cluster: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """What startup leaves behind: a pool opened from the descriptor the real
    reader accepted (``init_pool``'s own arguments), and the real DB layer."""
    monkeypatch.setattr(settings, "data_dir", cluster)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    pool = base._RetainingThreadedConnectionPool(1, 4, reserved=2, **settings.get_database_connection())
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(database, "get_connection", base._get_pg_connection)
    database.create_profile(PROFILE, "Preview descriptor", None, {"model": "sonnet"}, if_absent=True)
    try:
        yield cluster
    finally:
        pool.closeall()


def _missing(path: Path) -> None:
    path.unlink()


def _too_open(path: Path) -> None:
    path.chmod(0o644)


def _corrupt(path: Path) -> None:
    path.write_text("{")


def _directory(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _symlink(path: Path) -> None:
    moved = path.with_name(DESCRIPTOR + ".moved")
    path.rename(moved)
    path.symlink_to(moved)


def _hard_linked(path: Path) -> None:
    os.link(path, path.with_name(DESCRIPTOR + ".moved"))


FAULTS = {f.__name__.lstrip("_"): f for f in (_missing, _too_open, _corrupt, _directory, _symlink, _hard_linked)}


@pytest.fixture(params=list(FAULTS))
def failed_descriptor(request: pytest.FixtureRequest, worker: Path) -> Iterator[Callable[[], None]]:
    """Fail the file for the real reader, and put it back whatever the test did."""
    path = worker / DESCRIPTOR
    original = path.read_bytes()
    moved = path.with_name(DESCRIPTOR + ".moved")

    def fail() -> None:
        FAULTS[request.param](path)
        # The reader itself refuses it; nothing about the probe is stubbed.
        assert not settings.managed_database_available()
        with pytest.raises(RuntimeError, match="Managed PostgreSQL is not ready"):
            settings.get_database_connection()

    try:
        yield fail
    finally:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)
        path.write_bytes(original)
        path.chmod(0o600)
        assert settings.managed_database_available()


def _owner_session() -> str:
    session_id = "descriptor-" + uuid4().hex
    sessions.create_session(session_id, PROFILE, title="Preview descriptor", model="sonnet")
    return session_id


def _api_key_session() -> str:
    key = "descriptor-key-" + uuid4().hex
    database.create_api_user(key, key, key)
    session_id = "descriptor-" + uuid4().hex
    sessions.create_session(session_id, PROFILE, title="Preview descriptor", model="sonnet", api_user_id=key)
    return session_id


def _client() -> tuple[TestClient, list[str]]:
    app = FastAPI()
    app.include_router(auth.router)
    writes: list[str] = []

    @app.get("/api/v1/models")
    def models(token: str = Depends(require_auth)) -> dict[str, Any]:
        return {"auth": token}

    @app.post("/api/v1/settings/value")
    def protected_write(token: str = Depends(require_auth)) -> dict[str, Any]:
        writes.append(token)
        return {"auth": token}

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        authenticated, _ = await ws_auth.authenticate_websocket(websocket, None)
        if not authenticated:
            await websocket.close(code=4001)
            return
        await websocket.accept()
        await websocket.close()

    client = TestClient(IngressApplication(app, LOCAL, REMOTE, PREVIEW), raise_server_exceptions=False,
                        client=("172.17.0.2", 54321), follow_redirects=False)
    return client, writes


def _get(client: TestClient, port: int, path: str, token: str):
    return client.get(f"http://172.17.0.2:{port}{path}", cookies={preview_access.cookie_name(): token})


def _socket_refused(client: TestClient, port: int, token: str) -> int:
    """The close code a socket carrying ``token`` gets (0 when it was accepted)."""
    try:
        with client.websocket_connect(f"ws://172.17.0.2:{port}/ws",
                                      cookies={preview_access.cookie_name(): token},
                                      headers={"origin": f"http://172.17.0.2:{port}"}):
            return 0
    except WebSocketDisconnect as refused:
        return refused.code


def _assert_refused_everywhere(token: str) -> None:
    client, _ = _client()
    assert preview_access.verify(token) is None
    # The agent browser's own listener, HTTP and socket.
    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 403
    assert _socket_refused(client, PREVIEW, token) == 4403
    # The local listener's auth dependency and socket check.
    assert _get(client, LOCAL, "/api/v1/models", token).status_code == 401
    assert _socket_refused(client, LOCAL, token) == 4001
    # The redirect that would set the cookie.
    redirect = client.get(f"http://172.17.0.2:{PREVIEW}{preview_access.REDIRECT_PATH}",
                          params={preview_access.QUERY_PARAM: token})
    assert redirect.status_code == 403
    assert "set-cookie" not in redirect.headers


def test_session_deleted_during_a_descriptor_failure_loses_its_grant(failed_descriptor) -> None:
    session_id = _owner_session()
    token = preview_access.mint(session_id)
    assert preview_access.verify(token).session_id == session_id

    failed_descriptor()
    assert sessions.delete_session(session_id)

    _assert_refused_everywhere(token)


def test_session_deleted_before_a_descriptor_failure_stays_refused(failed_descriptor) -> None:
    session_id = _owner_session()
    token = preview_access.mint(session_id)
    assert sessions.delete_session(session_id)
    assert preview_access.verify(token) is None

    failed_descriptor()

    _assert_refused_everywhere(token)


def test_api_key_session_gets_no_grant_during_a_descriptor_failure(failed_descriptor) -> None:
    """A grant is the owner: signature-only would sign an API key's agent in as one."""
    token = preview_access.mint(_api_key_session())

    failed_descriptor()

    _assert_refused_everywhere(token)


def test_live_session_keeps_working_and_stays_limited_during_a_descriptor_failure(failed_descriptor) -> None:
    """The pool never needed the file again, so nothing here is an outage, and
    the grant's ceiling still holds."""
    token = preview_access.mint(_owner_session())
    client, writes = _client()
    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 200

    failed_descriptor()

    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 200
    assert _socket_refused(client, PREVIEW, token) == 0
    refused = client.post(f"http://172.17.0.2:{PREVIEW}/api/v1/settings/value",
                          cookies={preview_access.cookie_name(): token},
                          headers={"origin": f"http://172.17.0.2:{PREVIEW}"})
    assert refused.status_code == 403
    assert writes == []


def test_deletion_takes_effect_on_the_next_request_with_no_cached_grant(worker) -> None:
    """No grant survives between requests, with or without a healthy descriptor."""
    session_id = _owner_session()
    token = preview_access.mint(session_id)
    client, _ = _client()
    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 200
    assert sessions.delete_session(session_id)
    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 403


def test_database_gone_with_the_descriptor_refuses_at_the_listener(failed_descriptor, monkeypatch) -> None:
    """The supervisor stopping: no file and no pool is a refusal, never a pass."""
    token = preview_access.mint(_owner_session())
    client, _ = _client()

    failed_descriptor()
    monkeypatch.setattr(base, "_pg_pool", None)

    assert _get(client, PREVIEW, "/api/v1/models", token).status_code == 403
    assert _socket_refused(client, PREVIEW, token) == 4403
