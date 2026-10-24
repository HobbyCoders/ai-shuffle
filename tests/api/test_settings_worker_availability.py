"""Worker availability is owner-controlled, isolated by install and offloaded."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.api import settings as api
from app.api.auth import require_admin, require_auth
from app.core import desktop_listener
from app.core import worker_availability as availability
from app.core.config import settings
from app.db import database
from app.network import policy


@pytest.fixture
def client(test_db: Any, monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "instance_id", "DesktopWorker")
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setenv("SHUFFLE_DESKTOP", "1")
    monkeypatch.delenv("SHUFFLE_LAUNCHER_VERSION", raising=False)
    monkeypatch.setattr(desktop_listener, "configured", lambda: False)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as result:
        yield result


def test_owner_override_reset_and_worker_isolation(
    client: TestClient, monkeypatch: MonkeyPatch,
) -> None:
    app = client.app
    path = "/api/v1/settings/worker-availability"
    assert client.get(path).status_code in (401, 403)
    assert client.put(path, json={"always_on": True}).status_code in (401, 403)
    app.dependency_overrides[require_auth] = lambda: "owner"
    assert client.get(path).json() == {
        "worker_id": "DesktopWorker", "desktop": True,
        "always_on": False, "always_on_override": None,
    }
    assert client.put(path, json={"always_on": True}).status_code in (401, 403)
    app.dependency_overrides[require_admin] = lambda: "owner"
    assert client.put(path, json={"always_on": True}).json()["always_on"] is True
    assert database.get_system_setting(availability.setting_key()) == "true"
    assert policy.is_shared_setting_key(availability.setting_key())
    monkeypatch.setattr(settings, "instance_id", "ServerWorker")
    monkeypatch.setattr(settings, "local_mode", False)
    assert client.get(path).json()["always_on_override"] is None
    assert client.put(path, json={"always_on": False}).json()["always_on"] is False
    monkeypatch.setattr(settings, "instance_id", "DesktopWorker")
    monkeypatch.setattr(settings, "local_mode", True)
    assert client.get(path).json()["always_on_override"] is True
    for invalid in ({}, {"always_on": "true"}, {"always_on": 1}):
        assert client.put(path, json=invalid).status_code == 422
    assert client.put(path, json={"always_on": None}).json()["always_on"] is False
    assert database.get_system_setting(availability.setting_key()) is None


def test_database_work_is_offloaded(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    from app.api import settings_worker_availability as routes

    client.app.dependency_overrides[require_auth] = lambda: "owner"
    client.app.dependency_overrides[require_admin] = lambda: "owner"
    seen: list[int] = []
    loop_threads: list[int] = []
    snapshot, save, run_sync = routes.snapshot, routes.save, routes.run_sync

    async def offload(fn: Any, *args: Any) -> Any:
        loop_threads.append(threading.get_ident())
        return await run_sync(fn, *args)

    def read() -> availability.Availability:
        seen.append(threading.get_ident())
        return snapshot()

    def write(value: bool | None) -> availability.Availability:
        seen.append(threading.get_ident())
        return save(value)

    monkeypatch.setattr(routes, "run_sync", offload)
    monkeypatch.setattr(routes, "snapshot", read)
    monkeypatch.setattr(routes, "save", write)
    assert client.get("/api/v1/settings/worker-availability").status_code == 200
    response = client.put("/api/v1/settings/worker-availability", json={"always_on": True})
    assert response.status_code == 200
    assert len(seen) == len(loop_threads) == 2
    assert set(seen).isdisjoint(loop_threads)
