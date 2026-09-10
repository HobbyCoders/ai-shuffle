import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from app.core import connector_packages as packages
from app.core.connectors import get_connector_class
from app.core.connectors.base import ConnectorStatus

@pytest.fixture
def local_package(monkeypatch):
    sources = Path(__file__).parents[1] / "app" / "core" / "connectors"
    monkeypatch.setattr(packages, "_download", lambda name: (sources / name).read_bytes().replace(b"\r\n", b"\n"))
    # Offline unit tests; the live install check exercises actual pip/venv.
    monkeypatch.setattr(packages, "_prepare_environment", lambda *_: None)
    monkeypatch.setattr(packages, "_validate_worker", lambda *_: None)
    return packages

def test_catalog_installs_verified_sources(local_package):
    for kind in packages.CATALOG["packages"]:
        installed = packages.install(kind)
        assert installed.connector_type == kind
        assert packages.require_installed(kind) == installed.id
        assert get_connector_class(kind).__module__.endswith("package_proxy")
    with pytest.raises(ValueError): packages.install("../../evil")
    with pytest.raises(ValueError): packages.install("gmail", "project")

def test_failed_download_is_not_published(local_package, monkeypatch):
    monkeypatch.setattr(packages, "_download", lambda _: b"not trusted code")
    with pytest.raises(ValueError, match="integrity"): packages.install("gmail")
    assert packages.record("gmail") is None
    assert not list(packages.root().glob("download-*"))

def test_tampering_fails_closed(local_package):
    packages.install("webhook")
    path = packages.root() / "webhook" / packages.CATALOG["release"] / "webhook_connector.py"
    path.write_text("print('tampered')")
    with pytest.raises(ValueError, match="integrity"): packages.require_installed("webhook")
    with pytest.raises(ValueError, match="integrity"): get_connector_class("webhook")

def test_disable_uninstall_guard_saved_accounts(local_package, monkeypatch):
    packages.install("gmail")
    monkeypatch.setattr(packages, "dependencies", lambda _: [{"name": "Mail", "active": False, "automations": []}])
    for operation in [lambda: packages.set_enabled("gmail", False), lambda: packages.uninstall("gmail")]:
        with pytest.raises(ValueError, match="Mail"): operation()
    assert packages.record("gmail")["enabled"]
    monkeypatch.setattr(packages, "dependencies", lambda _: [])
    packages.set_enabled("gmail", False)
    with pytest.raises(ValueError, match="disabled"): get_connector_class("gmail")
    packages.set_enabled("gmail", True)
    packages.uninstall("gmail")
    assert packages.record("gmail") is None

def test_update_rollback_preserve_accounts(local_package, monkeypatch):
    packages.install("webhook")
    original = packages.record("webhook")
    catalog = copy.deepcopy(packages.CATALOG)
    catalog["release"] = "next-test-version"
    monkeypatch.setattr(packages, "CATALOG", catalog)
    refs = [{"id": "keep-id", "name": "Webhook", "active": True, "automations": []}]
    monkeypatch.setattr(packages, "dependencies", lambda _: refs)
    with pytest.raises(ValueError, match="Pause"): packages.install("webhook")
    assert packages.record("webhook") == original
    refs[0]["active"] = False
    packages.install("webhook")
    assert packages.record("webhook")["previous"] == {k: v for k, v in original.items() if k != "previous"}
    packages.rollback("webhook")
    assert packages.record("webhook")["release"] == original["release"]
    assert refs[0]["id"] == "keep-id"

@pytest.mark.asyncio
async def test_worker_delivers_actual_messages_triggers(local_package, monkeypatch):
    from app.core.connectors import package_proxy
    packages.install("webhook")
    monkeypatch.setattr(package_proxy, "interpreter", lambda _: Path(sys.executable))
    incoming, trigger = AsyncMock(), AsyncMock()
    instance = get_connector_class("webhook")(
        connector_id="stable-id", connector_type="webhook",
        config={"mode": "inbound", "inbound_path": "test", "inbound_token": "test-secret"},
        on_incoming_message=incoming, on_trigger=trigger, on_permission_response=None)
    try:
        assert await instance.connect()
        assert instance._process.pid
        assert instance.verify_inbound_request("{}", token="test-secret")
        assert not instance.verify_inbound_request("{}", token="wrong")
        await instance.handle_webhook_payload({"event": "hello"})
        assert incoming.await_args.args[0].connector_id == "stable-id"
        assert trigger.await_args.args[:2] == ("stable-id", "webhook_received")
    finally:
        await instance.disconnect()
    assert instance._process.returncode == 0
    assert instance.status == ConnectorStatus.DISCONNECTED
    assert await instance.connect()
    instance._process.kill()
    await instance._process.wait()
    await instance._reader
    assert instance.status == ConnectorStatus.ERROR
    await instance.disconnect()

@pytest.mark.asyncio
async def test_new_accounts_require_installed_runtime(local_package):
    from app.core.connector_service import create_connector
    with pytest.raises(ValueError, match="Install"):
        await create_connector(name="new", connector_type="webhook", config={"mode": "inbound"}, events=[])
    packages.install("webhook")
    result = await create_connector(name="new", connector_type="webhook", config={"mode": "inbound"}, events=[])
    assert result["config"]["runtime_plugin_id"] == "webhook@shuffle-connectors"
    assert packages.dependencies("webhook")[0]["id"] == result["id"]
    with pytest.raises(ValueError, match="connections"): packages.uninstall("webhook")

def test_canonical_plugin_api_lists_installs_and_guards_accounts(local_package):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.plugins import router
    from app.api.auth import require_auth, require_admin
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test"
    app.dependency_overrides[require_admin] = lambda: "test"
    with TestClient(app) as client:
        result = client.post("/api/v1/plugins/install", json={"plugin_id": "webhook@shuffle-connectors", "scope": "user"})
        assert result.status_code == 201, result.text
        assert result.json()["connector_type"] == "webhook"
        rows = client.get("/api/v1/plugins/installed").json()
        assert any(p["id"] == "webhook@shuffle-connectors" for p in rows)
        assert client.get("/api/v1/plugins/runtime/webhook@shuffle-connectors/connections").json() == []
        assert client.post("/api/v1/plugins/webhook@shuffle-connectors/disable").status_code == 200
        assert client.post("/api/v1/plugins/webhook@shuffle-connectors/enable").status_code == 200
        assert client.delete("/api/v1/plugins/webhook@shuffle-connectors").status_code == 204

def test_private_source_uses_existing_gh_auth_without_reading_token(monkeypatch):
    from app.core import auth
    from types import SimpleNamespace
    import subprocess
    calls = []
    monkeypatch.setattr(auth, "find_gh_executable", lambda: "gh")
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"source")
    monkeypatch.setattr(subprocess, "run", run)
    assert packages._download_private("gmail.py") == b"source"
    assert calls[0][1] == "api"
    assert packages.CATALOG["revision"] in calls[0][4]
    assert "token" not in calls[0]

def test_worker_environment_does_not_inherit_other_credentials(monkeypatch):
    from app.core.connectors.package_proxy import worker_environment
    monkeypatch.setenv("DATABASE_URL", "private-database")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider")
    monkeypatch.setenv("GH_TOKEN", "private-source-access")
    env = worker_environment()
    assert not {"DATABASE_URL", "OPENAI_API_KEY", "GH_TOKEN"} & env.keys()

@pytest.mark.asyncio
async def test_ready_worker_updates_saved_connection_status(local_package, monkeypatch):
    from app.core.connectors import package_proxy
    from app.core.connector_service import create_connector
    from app.db import database
    packages.install("webhook")
    monkeypatch.setattr(package_proxy, "interpreter", lambda _: Path(sys.executable))
    row = await create_connector(name="ready", connector_type="webhook", config={"mode":"inbound", "inbound_path":"ready"}, events=[])
    instance = get_connector_class("webhook")(connector_id=row["id"], connector_type="webhook", config=row["config"], on_incoming_message=None, on_permission_response=None)
    try:
        assert await instance.connect()
        assert database.get_connector(row["id"])["status"] == "connected"
    finally:
        await instance.disconnect()




