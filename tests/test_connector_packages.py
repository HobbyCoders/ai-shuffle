import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from app.core import connector_packages as packages
from app.core.connectors import get_connector_class
from app.core.connectors.base import ConnectorStatus



@pytest.mark.asyncio
async def test_watch_history_roundtrips_through_isolated_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from app.core.connectors import package_proxy
    from app.core.watch_connector_sources import poll

    # The fake SDK is inside a real isolated worker process. No vendor service,
    # installed connector package or native SDK is contacted.
    source = (
        'from typing import Any\n'
        'from app.core.connectors.base import BaseConnector, ConnectorStatus\n'
        'from app.core.connectors.watch_history import poll\n'
        '\n'
        '\n'
        'class FakeSDK:\n'
        '    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:\n'
        '        return {"messages": [{"ts": "200", "text": "important"}]\n'
        '                if kwargs["oldest"] < "200" else []}\n'
        '\n'
        '\n'
        'class FakeConnector(BaseConnector):\n'
        '    def __init__(self, connector_id: str, connector_type: str,\n'
        '                 config: dict[str, Any], **kwargs: Any) -> None:\n'
        '        super().__init__(connector_id, connector_type, config, **kwargs)\n'
        '        self._web_client = FakeSDK()\n'
        '        self._status = ConnectorStatus.CONNECTED\n'
        '\n'
        '    async def connect(self) -> bool:\n'
        '        return True\n'
        '\n'
        '    async def disconnect(self) -> None:\n'
        '        pass\n'
        '\n'
        '    async def send_message(self, *args: Any, **kwargs: Any) -> bool:\n'
        '        return True\n'
        '\n'
        '    send_permission_prompt = send_notification = send_message\n'
        '\n'
        '    def get_context_instructions(self, channel: str) -> str:\n'
        '        return ""\n'
        '\n'
        '    async def poll_watch(self, source: dict[str, Any], cursor: dict[str, Any]) -> Any:\n'
        '        return await poll(self.connector_type, self, source, cursor)\n'
    )
    (tmp_path / "fake_watch.py").write_text(source, encoding="utf-8")
    helper = Path(packages.__file__).with_name("connectors") / "watch_history.py"
    (tmp_path / "watch_history.py").write_bytes(helper.read_bytes())
    spec = {
        "module": "fake_watch", "class": "FakeConnector", "files": {
            filename: hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest()
            for filename in ("fake_watch.py", "watch_history.py")
        }
    }
    monkeypatch.setattr(package_proxy, "interpreter", lambda _: Path(sys.executable))
    cls = package_proxy.proxy_class("slack", tmp_path, spec)
    instance = cls("fake-watch", "slack", {}, AsyncMock(), AsyncMock())
    try:
        assert await instance.connect()
        assert not hasattr(instance, "_web_client")
        events, cursor = await poll("slack", instance, {"channel_id": "test"}, {"after": "100"})
        assert events[0][0] == "message_received:test:200"
        assert cursor == {"after": "200"}
        replay, checkpoint = await poll("slack", instance, {"channel_id": "test"}, cursor)
        assert replay == [] and checkpoint == cursor
    finally:
        await instance.disconnect()


@pytest.mark.asyncio
async def test_telegram_ingress_commit_ack_crosses_isolated_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from app.core.connectors import package_proxy
    from app.core.connectors.manager import connector_manager
    from app.core.automation_manager import automation_manager
    from app.db import watches as store

    source = (
        'from types import SimpleNamespace\n'
        'from typing import Any\n'
        'from app.core.connectors.telegram import TelegramConnector\n'
        'from app.core.connectors.base import ConnectorStatus\n'
        '\n'
        '\n'
        'class FakeConnector(TelegramConnector):\n'
        '    async def connect(self) -> bool:\n'
        '        self._status = ConnectorStatus.CONNECTED\n'
        '        return True\n'
        '\n'
        '    async def poll_watch(self, source: dict[str, Any], cursor: dict[str, Any]) -> Any:\n'
        '        await self._handle_message(SimpleNamespace(\n'
        '            chat=SimpleNamespace(id=123, type="group"),\n'
        '            from_user=SimpleNamespace(id=7, full_name="Test member"),\n'
        '            text="important", message_id=1,\n'
        '        ))\n'
        '        return [], cursor\n'
    )
    (tmp_path / "fake_telegram.py").write_text(source, encoding="utf-8")
    source_dir = Path(packages.__file__).with_name("connectors")
    for filename in ("telegram.py", "telegram_actions.py"):
        (tmp_path / filename).write_bytes((source_dir / filename).read_bytes())
    spec = {
        "module": "fake_telegram", "class": "FakeConnector", "files": {
            filename: hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest()
            for filename in ("fake_telegram.py", "telegram.py", "telegram_actions.py")
        }
    }
    # Retention keeps unread content only for watches that opted in before ingress.
    _, initial_cursor = store.connector_page("fake-telegram", "123", {})
    store.create(
        owner="admin", creating_session_id=None, worker_id="source", agent_id="agent",
        description="isolated Telegram ingress", source={"kind": "connector",
        "connector_id": "fake-telegram", "channel_id": "123"}, filter={}, action="alert",
        permissions={"read_only": True, "tools": []}, daily_budget_usd=1,
        cursor=initial_cursor,
    )
    persist = store.record_connector_event
    attempts = 0

    def record(connector_id: str, key: str, payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary persistence failure")
        persist(connector_id, key, payload)
        if attempts == 2:
            raise RuntimeError("commit succeeded, acknowledgement lost")

    monkeypatch.setattr(store, "record_connector_event", record)
    monkeypatch.setattr(automation_manager, "handle_connector_trigger", AsyncMock())
    monkeypatch.setattr(package_proxy, "interpreter", lambda _: Path(sys.executable))
    cls = package_proxy.proxy_class("telegram", tmp_path, spec)
    dispatched = asyncio.Event()
    incoming = AsyncMock(side_effect=lambda _: dispatched.set())
    instance = cls("fake-telegram", "telegram", {}, incoming, AsyncMock(),
                   on_trigger=connector_manager._handle_trigger)
    try:
        assert await instance.connect()
        await instance.poll_watch({}, {})
        assert attempts == 3
        await asyncio.wait_for(dispatched.wait(), timeout=2)
        incoming.assert_awaited_once()
        events, cursor = store.connector_page("fake-telegram", "123", initial_cursor)
        assert len(events) == 1
        assert events[0][1]["text"] == "important"
        assert store.connector_page("fake-telegram", "123", cursor) == ([], cursor)
    finally:
        await instance.disconnect()


def test_watch_helpers_are_shipped_in_verified_packages() -> None:
    for kind in ("slack", "discord"):
        assert "watch_history.py" in packages.CATALOG["packages"][kind]["files"]
        packages._verify(packages._SOURCE_DIR, kind)
    packages._verify(packages._SOURCE_DIR, "telegram")


@pytest.fixture
def local_package(monkeypatch):
    # Use the real bundled-source path. Only expensive runtime setup is stubbed;
    # the clean-install acceptance check also exercises actual pip/venv.
    monkeypatch.setattr(packages, "_prepare_environment", lambda *_: None)
    monkeypatch.setattr(packages, "_validate_worker", lambda *_: None)
    return packages

def test_catalog_installs_verified_sources(local_package):
    for kind in (p.connector_type for p in packages.available()):
        installed = packages.install(kind)
        assert installed.connector_type == kind
        assert packages.require_installed(kind) == installed.id
        assert get_connector_class(kind).__module__.endswith("package_proxy")
    with pytest.raises(ValueError): packages.install("../../evil")
    with pytest.raises(ValueError): packages.install("gmail", "project")
    with pytest.raises(ValueError, match="Unknown connector plugin"):
        packages.require_installed("unknown")

def test_teams_experimental_opt_in(local_package, monkeypatch):
    from app.core.connector_service import get_connector_type_info

    monkeypatch.setenv("AI_SHUFFLE_EXPERIMENTAL_MSTEAMS", "true")
    assert not packages.is_offered("msteams")
    monkeypatch.setenv("AI_SHUFFLE_EXPERIMENTAL_MSTEAMS", "1")
    assert packages.is_offered("msteams")
    assert "msteams" in {p.connector_type for p in packages.available()}
    assert "msteams" in {t["type"] for t in get_connector_type_info()}
    assert packages.install("msteams").id == packages.require_installed("msteams")
    assert packages.CATALOG["packages"]["msteams"]["offered"] is False


def test_teams_is_retained_but_not_offered_or_installable(local_package):
    from app.core.connector_service import get_connector_type_info

    assert packages.CATALOG["packages"]["msteams"]["offered"] is False
    packages._verify(packages._SOURCE_DIR, "msteams")
    assert "msteams" not in {p.connector_type for p in packages.available()}
    assert "msteams" not in {t["type"] for t in get_connector_type_info()}
    for operation in (packages.install, packages.require_installed):
        with pytest.raises(ValueError, match="Microsoft Teams is not available yet"):
            operation("msteams")
    assert packages.record("msteams") is None


def test_existing_teams_package_can_be_removed_but_not_reinstalled(local_package, monkeypatch):
    catalog = copy.deepcopy(packages.CATALOG)
    catalog["packages"]["msteams"]["offered"] = True
    with monkeypatch.context() as patch:
        patch.setattr(packages, "CATALOG", catalog)
        packages.install("msteams")
    assert "msteams" in {p.connector_type for p in packages.installed()}
    for operation in (packages.install, packages.require_installed):
        with pytest.raises(ValueError, match="Microsoft Teams is not available yet"):
            operation("msteams")
    packages.uninstall("msteams")
    assert packages.record("msteams") is None


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
    with pytest.raises(ValueError, match="Microsoft Teams is not available yet"):
        await create_connector(name="Teams", connector_type="msteams", config={}, events=[])
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
        result = client.post("/api/v1/plugins/install", json={"plugin_id": "msteams@shuffle-connectors", "scope": "user"})
        assert result.status_code == 400, result.text
        assert "Microsoft Teams is not available yet" in result.json()["detail"]
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
    for kind in (p.connector_type for p in packages.available()):
        packages.install(kind)
        assert packages.record(kind)["revision"] == packages.CATALOG["revision"]
    previous = {p.connector_type: packages.record(p.connector_type) for p in packages.available()}
    catalog = copy.deepcopy(packages.CATALOG)
    catalog["release"] = "next-test-version"
    monkeypatch.setattr(packages, "CATALOG", catalog)
    for kind in previous:
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


def test_failed_runtime_check_names_the_step_and_logs_the_cause(local_package, monkeypatch, caplog):
    import logging
    import subprocess

    def broken(*_):
        raise subprocess.CalledProcessError(1, ["python"], stderr=b"ModuleNotFoundError: No module named 'app.core.x'")

    monkeypatch.setattr(packages, "_validate_worker", broken)
    with caplog.at_level(logging.WARNING, logger=packages.__name__):
        with pytest.raises(ValueError, match="start-up check") as raised:
            packages.install("signal")
    assert "PyPI" not in str(raised.value)
    assert "No module named 'app.core.x'" in caplog.text
    assert packages.record("signal") is None

    def offline(*_):
        raise subprocess.CalledProcessError(1, ["pip"], stderr=b"Could not find a version")

    monkeypatch.setattr(packages, "_prepare_environment", offline)
    with caplog.at_level(logging.WARNING, logger=packages.__name__):
        with pytest.raises(ValueError, match="PyPI"):
            packages.install("signal")
    assert "Could not find a version" in caplog.text
