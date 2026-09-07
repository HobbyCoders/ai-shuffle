"""Resource sync contracts: isolated SQLite, real common mutation boundaries."""
import asyncio
import importlib
import inspect
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.global_events import GlobalEvents, global_events
from app.core.resource_sync import mutation, observe_transactions
from app.db import database, notifications

INVENTORY = {'projects': ('projects', 'shared', 'create_project update_project delete_project set_project_link_group'), 'projects_v2': ('projects', 'shared', 'upsert_project'), 'profiles': ('agents', 'shared', 'create_profile update_profile delete_profile set_profile_builtin'), 'subagents': ('agents', 'shared', 'create_subagent update_subagent delete_subagent set_subagent_builtin revert_subagent_to_defaults add_permission_rule delete_permission_rule delete_profile_permission_rules'), 'tags': ('tags', 'shared', 'create_tag update_tag delete_tag add_session_tag remove_session_tag set_session_tags'), 'notes': ('notes', 'shared', 'create_note update_note delete_note bulk_update_note_file_paths create_note_attachment set_note_attachment_data delete_note_attachment'), 'templates': ('templates', 'shared', 'create_template update_template delete_template set_template_builtin'), 'connectors': ('connectors', 'admin', 'create_connector update_connector update_connector_status delete_connector claim_connector_runner update_connector_event_dispatched create_connector_event_log create_connector_session update_connector_session_ai_session'), 'automations': ('automations', 'admin', 'create_automation update_automation delete_automation record_automation_run_success claim_automation_fire create_automation_run update_automation_run create_automation_approval update_automation_approval'), 'hooks': ('hooks', 'admin', 'create_hook update_hook delete_hook update_hook_triggered'), 'notifications': ('notifications', 'notification', 'create_notification mark_read mark_all_read dismiss clear_all'), 'sessions': ('sessions', 'session', 'create_session create_session_with_stats update_session set_session_run_binding adopt_session adopt_session_follow unpin_session pin_session_placement set_session_project set_session_branch resync_session_branch update_session_pr_state set_session_parked_task_kind set_working_set_ref delete_session set_session_active set_session_hidden clear_session_conversation delete_session_message delete_orphan_user_turn discard_orphan_first_turn delete_session_messages_after delete_session_messages_after_jsonl_uuid')}


INVENTORY['api_users'] = ('api_users', 'admin', 'create_api_user update_api_user delete_api_user update_credential_policy set_user_credential_policy delete_user_credential_policy set_user_credential delete_user_credential')


@pytest.mark.parametrize("module,resource,scope,name", [
    (module, resource, scope, name)
    for module, (resource, scope, names) in INVENTORY.items()
    for name in names.split()
])
def test_common_boundary_inventory(module, resource, scope, name):
    implementation = getattr(importlib.import_module("app.db." + module), name)
    assert implementation.resource_sync == (resource, scope)
    assert inspect.signature(implementation) == inspect.signature(implementation.__wrapped__)
    if hasattr(database, name):
        assert getattr(database, name) is implementation


@pytest.fixture
def events():
    with patch.object(global_events, "publish") as publish:
        yield publish


def resources(events):
    return [call.args[0]["resources"] for call in events.call_args_list]


@pytest.mark.parametrize("resource,create,update,delete", [
    ("projects", lambda: database.create_project("x", "one", None, "/tmp/x"),
     lambda: database.update_project("x", name="two"), lambda: database.delete_project("x")),
    ("agents", lambda: database.create_profile("x", "one", None, {}),
     lambda: database.update_profile("x", name="two"), lambda: database.delete_profile("x")),
    ("tags", lambda: database.create_tag("x", "one"),
     lambda: database.update_tag("x", name="two"), lambda: database.delete_tag("x")),
    ("notes", lambda: database.create_note("x", "one"),
     lambda: database.update_note("x", title="two"), lambda: database.delete_note("x")),
    ("templates", lambda: database.create_template("x", "one", "prompt"),
     lambda: database.update_template("x", name="two"), lambda: database.delete_template("x")),
    ("connectors", lambda: database.create_connector("x", "slack", "one", {}, []),
     lambda: database.update_connector("x", name="two"), lambda: database.delete_connector("x")),
    ("hooks", lambda: database.create_hook("x", "one", "PreToolUse", "bash", command="true"),
     lambda: database.update_hook("x", name="two"), lambda: database.delete_hook("x")),
    ("automations", lambda: database.create_automation("x", "one", "cron", {}, "fixture-profile"),
     lambda: database.update_automation("x", name="two"), lambda: database.delete_automation("x")),
])
def test_store_crud_and_missing_delete(events, resource, create, update, delete):
    database.create_profile("fixture-profile", "Fixture", None, {})
    for operation in (create, update, delete):
        events.reset_mock()
        assert operation()
        assert resources(events)
        assert all(item == [resource] for item in resources(events))
        shared = resource in {"projects", "agents", "tags", "notes", "templates"}
        assert {c.kwargs["user_type"] for c in events.call_args_list} == (
            {"admin", "api_user", "account", "paired"} if shared else {"admin", "account", "paired"})
    events.reset_mock()
    assert not delete()
    events.assert_not_called()


def test_system_settings_initialization_noop_and_delete(events):
    from app.db import settings
    settings.set_system_setting("sync-test", "secret value")
    assert resources(events) == [["settings"]] * 3
    events.reset_mock()
    assert settings.get_or_create_system_setting("sync-test", lambda: "ignored") == "secret value"
    events.assert_not_called()
    assert settings.delete_system_setting("sync-test")
    assert resources(events) == [["settings"]] * 3
    events.reset_mock()
    assert not settings.delete_system_setting("sync-test")
    events.assert_not_called()


def test_notification_mutations_user_wide_and_noop(events):
    row = notifications.create_notification("alice", message="private", notification_id="n")
    assert row["message"] == "private"
    assert resources(events) == [["notifications"]] * 3
    assert events.call_args.kwargs == {"user_type": "api_user", "user_id": "alice"}
    events.reset_mock()
    notifications.create_notification("alice", message="ignored", notification_id="n")
    assert not notifications.mark_read("bob", "n")
    assert notifications.mark_all_read("bob") == 0
    events.assert_not_called()
    for operation in (lambda: notifications.mark_read("alice", "n"),
                      lambda: notifications.dismiss("alice", "n"),
                      lambda: notifications.clear_all("alice")):
        events.reset_mock()
        assert operation()
        assert resources(events) == [["notifications"]] * 3
        assert events.call_args.kwargs["user_id"] == "alice"


def test_rollback_commit_failure_and_no_update(events):
    from app.db import tags
    from app.db.base import get_db
    @contextmanager
    def failing():
        with get_db() as conn:
            yield conn
            raise RuntimeError("commit failure")
    with patch.object(tags, "get_db", observe_transactions(failing)):
        with pytest.raises(RuntimeError, match="commit failure"):
            tags.create_tag("bad", "rollback")
    events.assert_not_called()
    assert tags.get_tag("bad") is None
    tags.create_tag("good", "good")
    events.reset_mock()
    assert tags.update_tag("good")
    assert tags.update_tag("missing", name="changed") is None
    with pytest.raises(Exception):
        tags.create_tag("good", "duplicate")
    events.assert_not_called()


def test_publish_occurs_after_transaction_exit(events, mock_db):
    def check(message, **scope):
        assert not mock_db.in_transaction
        assert database.get_tag("post")["name"] == "committed"
    events.side_effect = check
    database.create_tag("post", "committed")
    assert events.call_count == 4


def test_session_metadata_not_token_stream(events):
    database.create_profile("p", "Profile", None, {})
    database.create_api_user("admin", "API owner", "test-hash")
    database.create_session("s", "p", api_user_id="admin")
    events.reset_mock()
    database.update_session("s", title="renamed")
    assert resources(events) == [["sessions"]] * 4
    assert events.call_args.kwargs == {"user_type": "api_user", "user_id": "admin"}
    events.reset_mock()
    database.set_session_hidden("s", True)
    assert resources(events) == [["sessions"]] * 4
    events.reset_mock()
    database.add_session_message("s", "assistant", "token")
    events.assert_not_called()


class Socket:
    def __init__(self):
        self.sent = []
        self.close = AsyncMock()
    async def send_text(self, text):
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_thread_publication_isolates_same_string_id_and_multiple_devices():
    hub = GlobalEvents()
    sockets = [Socket() for _ in range(4)]
    for socket, kind, identity in zip(sockets, ["admin", "api_user", "api_user", "api_user"],
                                     ["same", "same", "same", "other"]):
        hub.register(socket, kind, identity)
    try:
        await asyncio.to_thread(hub.resources_changed, "notifications", user_type="api_user", user_id="same")
        for _ in range(12):
            await asyncio.sleep(0)
        assert sockets[1].sent == sockets[2].sent
        assert len(sockets[1].sent) == 2
        assert sockets[0].sent == sockets[3].sent == [{"type": "sync_ready"}]
    finally:
        await hub.shutdown()


def test_notification_explicit_api_type_for_admin_string(events):
    notifications.create_notification(notifications.NotificationOwner("admin", "api_user"), message="x")
    assert events.call_args.kwargs == {"user_type": "api_user", "user_id": "admin"}


def test_rest_store_uses_same_boundary(events):
    from app.api import tags
    from app.api.auth import require_auth
    app = FastAPI()
    app.include_router(tags.router)
    app.dependency_overrides[require_auth] = lambda: "token"
    with TestClient(app) as client:
        response = client.post("/api/v1/tags", json={"name": "REST", "color": "#112233"})
        assert response.status_code == 201, response.text
        assert resources(events) == [["tags"]] * 4
        events.reset_mock()
        response = client.delete("/api/v1/tags/" + response.json()["id"])
        assert response.status_code == 204, response.text
        assert resources(events) == [["tags"]] * 4


@pytest.mark.asyncio
async def test_app_config_hook_tool_common_boundary(events):
    from app.mcp.config_hooks import _create_hook_tools
    tool = next(t for t in _create_hook_tools(True, None) if t.name == "createHook")
    result = await tool.handler({"name": "tool", "event": "PreToolUse", "hook_type": "bash", "command": "true", "tool_matcher": "Bash"})
    assert not result.get("isError"), result
    assert resources(events) == [["hooks"]] * 3


def test_mcp_durable_success_and_failure(events, tmp_path):
    from app.core.mcp_service import McpService, McpScope, McpServerConfig
    service = McpService(tmp_path)
    service.USER_MCP_FILE = tmp_path / "user.json"
    service.SETTINGS_FILE = tmp_path / "settings.json"
    config = McpServerConfig(name="test", command="test-command")
    service.add_server("test", config, McpScope.USER)
    assert json.loads(service.USER_MCP_FILE.read_text())["mcpServers"]["test"]["command"] == "test-command"
    assert resources(events) == [["mcp"]] * 4
    events.reset_mock()
    with patch.object(service, "_write_json_file", return_value=False):
        with pytest.raises(RuntimeError):
            service.remove_server("test")
    events.assert_not_called()
    assert service.remove_server("test")
    assert resources(events) == [["mcp"]] * 4


def test_plugin_writes_and_failed_results(events, tmp_path):
    from app.core.plugin_service import PluginService
    with patch("app.core.plugin_service._get_fluid_dir", return_value=tmp_path):
        service = PluginService()
    assert service.enable_plugin("test")
    assert json.loads(service.SETTINGS_FILE.read_text())["enabledPlugins"]["test"] is True
    assert resources(events) == [["plugins"]] * 4
    events.reset_mock()
    with patch.object(service, "_write_json_file", return_value=False):
        assert not service.disable_plugin("test")
    events.assert_not_called()


@pytest.mark.parametrize("result", [False, None, {}, [], {"success": False}, {"error": "failed"}])
def test_service_failure_shapes(result, events):
    @mutation("plugins", database=False)
    def write():
        return result
    assert write() is result
    events.assert_not_called()


@pytest.mark.asyncio
async def test_async_signature_and_result_semantics(events):
    @mutation("mcp", database=False)
    async def write(value: str, *, enabled=True):
        return value
    assert inspect.iscoroutinefunction(write)
    assert inspect.signature(write) == inspect.signature(write.__wrapped__)
    assert await write("ok") == "ok"
    assert resources(events) == [["mcp"]] * 3

@pytest.mark.parametrize("module,cls,resource,names", [
    ("mcp_service", "McpService", "mcp", "add_server update_server remove_server set_server_enabled set_project_server_enabled add_project_installed_server remove_project_installed_server"),
    ("plugin_service", "PluginService", "plugins", "add_marketplace remove_marketplace sync_marketplace sync_all_marketplaces install_plugin install_plugins_batch uninstall_plugin enable_plugin disable_plugin install_plugin_to_project uninstall_project_plugin enable_project_plugin disable_project_plugin update_plugin update_all_plugins save_auto_update_settings"),
])
def test_service_inventory(module, cls, resource, names):
    service = getattr(importlib.import_module("app.core." + module), cls)
    for name in names.split():
        function = getattr(service, name)
        assert function.resource_sync == (resource, "shared")
        assert inspect.signature(function) == inspect.signature(function.__wrapped__)


def test_real_commit_exception_has_no_event(events):
    from app.db import base, tags
    original = base.get_connection
    def connection():
        conn = original()
        conn.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit rejected"))
        return conn
    with patch.object(base, "get_connection", connection):
        with pytest.raises(RuntimeError, match="commit rejected"):
            tags.create_tag("failed", "failed")
    events.assert_not_called()


def test_swallowed_file_write_failure_and_failed_batch(events):
    from app.core.resource_sync import install_service_mutations
    class Service:
        def _write_json_file(self):
            return False
        def install(self):
            self._write_json_file()
            return {"success": True}
        def batch(self):
            return {"one": False, "two": False}
    install_service_mutations(Service, "plugins", "install batch")
    assert Service().install() == {"success": True}
    assert Service().batch() == {"one": False, "two": False}
    events.assert_not_called()

def test_nested_agent_facades_coalesce(events):
    row = database.create_subagent("nested", "Nested", "description", "prompt")
    assert row["id"] == "nested"
    assert resources(events) == [["agents"]] * 4


def test_committed_rows_are_invalidated_even_when_later_work_raises(events):
    @mutation('tags', scope='shared')
    def partially_successful():
        database.create_tag('committed-before-error', 'kept')
        raise RuntimeError('later operation failed')
    with pytest.raises(RuntimeError, match='later operation failed'):
        partially_successful()
    assert database.get_tag('committed-before-error')['name'] == 'kept'
    assert resources(events) == [['tags']] * 4


def test_api_user_crud_invalidates_after_commit_but_usage_touch_does_not(events):
    database.create_api_user('u', 'User', 'private-key-hash')
    assert resources(events) == [['api_users']] * 3
    assert 'private-key-hash' not in str(events.call_args_list)
    events.reset_mock()
    database.update_api_user_last_used('u')
    events.assert_not_called()
    database.update_api_user('u', name='Updated')
    assert resources(events) == [['api_users']] * 3
    events.reset_mock()
    assert database.delete_api_user('u')
    assert resources(events) == [['api_users']] * 3
    events.reset_mock()
    assert not database.delete_api_user('u')
    events.assert_not_called()


def test_project_memory_save_delete_and_failed_write_invalidation(events, tmp_path):
    from app.core.project_memory_service import ProjectMemoryService
    from pathlib import Path
    service = ProjectMemoryService(tmp_path)
    saved = service.save('project', 'private memory')
    assert saved.content == 'private memory'
    assert resources(events) == [['projects']] * 4
    assert 'private memory' not in str(events.call_args_list)
    events.reset_mock()
    with patch.object(Path, 'write_text', side_effect=OSError('read-only')):
        with pytest.raises(OSError):
            service.save('project', 'not written')
    events.assert_not_called()
    assert service.delete('project')
    assert resources(events) == [['projects']] * 4
    events.reset_mock()
    with pytest.raises(ValueError):
        service.delete('project')
    events.assert_not_called()


def test_notification_rest_keeps_explicit_audience(events):
    from app.api import notifications as api
    from app.api.auth import require_auth
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "token"
    with patch.object(api, "get_api_user_from_request", return_value={"id": "admin"}):
        with TestClient(app) as client:
            response = client.post("/api/v1/notifications", json={"message": "private"})
            assert response.status_code == 200
            assert events.call_args.kwargs == {"user_type": "api_user", "user_id": "admin"}
            assert resources(events) == [["notifications"]] * 3
            assert events.call_args.args[0] == {"type": "resources_changed", "resources": ["notifications"]}
