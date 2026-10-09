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
    # Use the real bundled-source path. Only expensive runtime setup is stubbed;
    # the clean-install acceptance check also exercises actual pip/venv.
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

def test_discord_bot_name_preserves_plugin_identity(local_package):
    available = next(p for p in packages.available() if p.connector_type == "discord")
    installed = packages.install("discord")
    for plugin in (available, installed, packages.info("discord"), packages.details("discord")):
        assert plugin.name == "Discord Bot"
        assert plugin.id == "discord@shuffle-connectors"
        assert plugin.connector_type == "discord"
    assert packages.require_installed("discord") == installed.id


def test_bad_source_is_not_published(local_package, monkeypatch):
    monkeypatch.setattr(packages, "_bundled_source", lambda _: b"not trusted code")
    with pytest.raises(ValueError, match="integrity"): packages.install("gmail")
    assert packages.record("gmail") is None
    assert not list(packages.root().glob("stage-*"))

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

@pytest.fixture
def buyer_without_github(monkeypatch, tmp_path):
    import httpx
    import subprocess
    from app.core.auth import auth_service

    monkeypatch.setenv("HOME", str(tmp_path / "clean-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "clean-home"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx.Client, "send", lambda *_args, **_kwargs: pytest.fail("connector source must not use HTTP"))
    monkeypatch.setattr(auth_service, "_github_env", lambda: pytest.fail("connector install must not access GitHub credentials"))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("source staging must not invoke gh"))


def test_all_connectors_install_and_update_without_github(local_package, buyer_without_github, monkeypatch):
    for kind in packages.CATALOG["packages"]:
        packages.install(kind)
        assert packages.record(kind)["revision"] == packages.CATALOG["revision"]
    previous = {kind: packages.record(kind) for kind in packages.CATALOG["packages"]}
    catalog = copy.deepcopy(packages.CATALOG)
    catalog["release"] = "next-test-version"
    monkeypatch.setattr(packages, "CATALOG", catalog)
    for kind in catalog["packages"]:
        packages.install(kind)
        assert packages.record(kind)["release"] == catalog["release"]
        assert packages.record(kind)["previous"]["release"] == previous[kind]["release"]
        packages.rollback(kind)
        assert packages.record(kind)["release"] == previous[kind]["release"]


@pytest.mark.parametrize("updating", [False, True])
@pytest.mark.parametrize("fault", ["digest", "oversized", "missing", "symlink", "crlf"])
def test_damaged_bundle_fails_closed_without_replacing_runtime(local_package, buyer_without_github, monkeypatch, tmp_path, updating, fault):
    import shutil
    if updating:
        packages.install("webhook")
    previous = packages.record("webhook")
    catalog = copy.deepcopy(packages.CATALOG)
    catalog["release"] = "next-test-version"
    monkeypatch.setattr(packages, "CATALOG", catalog)
    bundled = tmp_path / "shipped-connectors"
    shutil.copytree(packages._SOURCE_DIR, bundled)
    monkeypatch.setattr(packages, "_SOURCE_DIR", bundled)
    source = bundled / "webhook_connector.py"
    expected = "integrity"
    if fault == "digest":
        source.write_bytes(b"tampered")
    elif fault == "oversized":
        source.write_bytes(b"x" * (packages._MAX_FILE_BYTES + 1))
        expected = "size limit"
    elif fault == "missing":
        source.unlink()
    elif fault == "symlink":
        real = tmp_path / "other-source.py"
        source.replace(real)
        try:
            source.symlink_to(real)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
    else:
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match=expected):
        packages.install("webhook")
    assert packages.record("webhook") == previous
    assert not list(packages.root().glob("stage-*"))
    assert not (packages.root() / "webhook" / catalog["release"]).exists()
    if previous:
        assert packages.require_installed("webhook") == "webhook@shuffle-connectors"


@pytest.mark.parametrize("name", ["../source.py", "/source.py", r"..\source.py", "", ".", ".."])
def test_source_filenames_cannot_leave_bundle(name):
    with pytest.raises(ValueError, match="filename"):
        packages._bundled_source(name)


def test_installed_sources_retain_size_limit(local_package):
    packages.install("webhook")
    source = packages.root() / "webhook" / packages.CATALOG["release"] / "webhook_connector.py"
    source.write_bytes(b"x" * (packages._MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        packages.require_installed("webhook")


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






def test_catalog_sources_import_only_what_the_worker_provides():
    # The isolated worker has no app package: sdk() stands in for the few app
    # modules a connector may import. A connector importing anything else fails
    # to install (#2188); install tests stub the worker, so check it here.
    import ast
    import json
    import subprocess
    connectors = Path(packages.__file__).parent / "connectors"
    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('package_worker', sys.argv[1])\n"
        "worker = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(worker)\n"
        "worker.sdk(worker.Path(sys.argv[2]))\n"
        "worker.OUTPUT.write(json.dumps({n: dir(m) for n, m in sys.modules.items() if n.startswith('app')}))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(connectors / "package_worker.py"), str(connectors)],
                         capture_output=True, text=True, check=True).stdout
    provided = json.loads(out)
    problems = []
    for kind, spec in packages.CATALOG["packages"].items():
        own = {"app.core.connectors." + Path(name).stem for name in spec["files"]}
        for name in spec["files"]:
            for node in ast.walk(ast.parse((connectors / name).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imports = [(alias.name, None) for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports = [(node.module, alias.name) for alias in node.names]
                else:
                    continue
                for module, attr in imports:
                    if module.split(".")[0] != "app" or module in own:
                        continue
                    if module not in provided:
                        problems.append(f"{kind}: {name} imports {module}")
                    elif attr and attr not in provided[module] and f"{module}.{attr}" not in own:
                        problems.append(f"{kind}: {name} imports {attr} from {module}")
    assert not problems, problems
