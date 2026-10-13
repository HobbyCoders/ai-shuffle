"""Connectors REST API flows, through the real app and TestClient.

Covers every route in docs/content/api-reference/connectors.md: the owner-only
boundary, create/update/delete and their effect on the live instance, the
lifecycle actions, the inspection routes, the public webhooks, and the Google
and Plaid sign-in flows.

Hermetic: the SQLite shim backs the database, a fake connector manager stands in
for the live clients (it records every start/stop/restart), and nothing here
opens a network connection.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.api import connector_oauth as google
from app.api import connector_plaid as plaid
from app.core import encryption
from app.core.connector_service import (
    ALLOWLIST_REQUIRED_MESSAGE,
    decrypt_config,
    get_connector_type_info,
)
from app.core.models import VALID_CONNECTOR_EVENTS
from app.db import database
from app.db.base import get_db

BASE = "/api/v1/connectors"
OWNER_REQUIRED = {"detail": "Owner access required"}


# =============================================================================
# Fixtures
# =============================================================================


class FakeManager:
    """Stands in for ConnectorManager: records calls, never connects out."""

    def __init__(self):
        self.calls = []
        self.running = {}
        self.start_ok = True
        self.start_error = None

    # App startup/shutdown hooks: nothing to connect in a test.
    async def start_all(self):
        pass

    def start_event_relay(self):
        pass

    async def shutdown_all(self):
        pass

    async def start_connector(self, connector_id):
        self.calls.append(("start", connector_id))
        if not self.start_ok:
            if self.start_error:
                database.update_connector_status(connector_id, "error", error=self.start_error)
            return False
        row = database.get_connector(connector_id)
        self.running.setdefault(connector_id, SimpleNamespace(connector_type=row["type"]))
        return True

    async def stop_connector(self, connector_id):
        self.calls.append(("stop", connector_id))
        self.running.pop(connector_id, None)

    async def restart_connector(self, connector_id):
        self.calls.append(("restart", connector_id))
        return True

    async def get_running_connector(self, connector_id):
        return self.running.get(connector_id)

    def get_running_connectors(self, connector_type=None):
        for cid, instance in list(self.running.items()):
            if connector_type is None or instance.connector_type == connector_type:
                yield cid, instance

    def get_connector_status(self, connector_id):
        return {"connector_id": connector_id} if connector_id in self.running else None


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """Real Fernet encryption so secrets round-trip exactly as in production."""
    from cryptography.fernet import Fernet
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())


@pytest.fixture(autouse=True)
def _clean_oauth_state(monkeypatch):
    for key in ("SHUFFLE_GOOGLE_CLIENT_ID", "SHUFFLE_GOOGLE_CLIENT_SECRET", "SHUFFLE_GOOGLE_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    google._pending.clear()
    plaid._pending.clear()
    yield
    google._pending.clear()
    plaid._pending.clear()


@pytest.fixture
def fake_manager(monkeypatch):
    from app.core.connectors import manager as manager_module
    fake = FakeManager()
    monkeypatch.setattr(manager_module, "connector_manager", fake)
    return fake


@pytest.fixture
def make_client(mock_db, _api_unit_credentials):
    """Build TestClients on the real app, as the owner or with no credentials."""
    from app.main import app
    from app.core.automation_manager import automation_manager

    automation_manager._scheduler = None
    automation_manager._initialized = False
    opened = []

    def make(base_url="http://testserver", owner=True):
        if owner:
            app.dependency_overrides[auth.require_admin] = lambda: "owner"
        client = TestClient(app, base_url=base_url)
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)
    app.dependency_overrides.pop(auth.require_admin, None)


@pytest.fixture
def owner(make_client, bundled_connector_catalog, fake_manager):
    return make_client()


def create(client, name="Bot", type_="webhook", config=None, **extra):
    body = {"name": name, "type": type_, "events": [], **extra}
    if config is None and type_ == "webhook":
        config = {"mode": "inbound", "inbound_path": name.lower(), "inbound_token": "hook-token"}
    body["config"] = config or {}
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def set_runner(connector_id, runner_id):
    with get_db() as conn:
        conn.cursor().execute("UPDATE connectors SET runner_id = %s WHERE id = %s", (runner_id, connector_id))


def add_automation(automation_id, connector_id, *, enabled=True, name=None, output=False):
    trigger_config = {} if connector_id is None else {"connector_id": connector_id}
    database.create_automation(
        automation_id=automation_id, name=name or automation_id, trigger_type="connector_event",
        trigger_config=trigger_config, profile_id="default", prompt="Summarise",
        output_config={"type": "connector", "connector_id": connector_id} if output else None,
        enabled=enabled,
    )


# =============================================================================
# Owner-only boundary
# =============================================================================


OWNER_ROUTES = [
    ("GET", "/types", None),
    ("GET", "/events", None),
    ("GET", "", None),
    ("POST", "", {"name": "x", "type": "webhook", "config": {}, "events": []}),
    ("POST", "/discord/discover", {"bot_token": "abc"}),
    ("GET", "/{cid}", None),
    ("PATCH", "/{cid}", {"name": "renamed"}),
    ("DELETE", "/{cid}", None),
    ("GET", "/{cid}/dependencies", None),
    ("POST", "/{cid}/test", None),
    ("POST", "/{cid}/activate", None),
    ("POST", "/{cid}/deactivate", None),
    ("POST", "/{cid}/restart", None),
    ("GET", "/{cid}/status", None),
    ("GET", "/{cid}/logs", None),
    ("GET", "/{cid}/triggers", None),
    ("GET", "/{cid}/actions", None),
    ("GET", "/{cid}/tools", None),
    ("POST", "/{cid}/actions/send_webhook/execute", {}),
    ("GET", "/oauth/google/status", None),
    ("PUT", "/oauth/google/app", {"client_id": "c", "client_secret": "s", "redirect_uri": "http://localhost/x"}),
    ("POST", "/oauth/google/start", {}),
    ("GET", "/oauth/plaid/status", None),
    ("PUT", "/oauth/plaid/app", {"client_id": "c", "client_secret": "s", "redirect_uri": "https://x/plaid-link"}),
    ("POST", "/oauth/plaid/start", None),
    ("POST", "/oauth/plaid/exchange", {"flow_id": "f", "public_token": "p"}),
]


class TestOwnerBoundary:
    @pytest.mark.parametrize("method,path,body", OWNER_ROUTES, ids=[f"{m} {p}" for m, p, _ in OWNER_ROUTES])
    def test_api_user_key_is_refused_before_any_work(self, make_client, bundled_connector_catalog,
                                                     fake_manager, method, path, body):
        database.create_api_user("integration", "Integration", auth.hash_api_key("aih_integration"),
                                 profile_ids=["default"])
        owner = make_client()
        cid = create(owner, name="Guarded")["id"]
        from app.main import app
        app.dependency_overrides.pop(auth.require_admin, None)
        key_client = make_client(owner=False)

        response = key_client.request(method, BASE + path.format(cid=cid), json=body,
                                      headers={"Authorization": "Bearer aih_integration"})

        assert response.status_code == 403, response.text
        assert response.json() == OWNER_REQUIRED
        # Nothing moved: the connector is untouched and no live client was touched.
        row = database.get_connector(cid)
        assert row["name"] == "Guarded" and row["is_active"] in (False, 0)
        assert fake_manager.calls == []

    def test_no_credentials_is_refused_too(self, make_client, bundled_connector_catalog, fake_manager):
        anonymous = make_client(owner=False)
        response = anonymous.get(BASE + "/types")
        assert response.status_code == 403
        assert response.json() == OWNER_REQUIRED

    def test_public_webhooks_do_not_ask_for_owner_access(self, make_client, fake_manager):
        anonymous = make_client(owner=False)
        response = anonymous.post(BASE + "/hooks/nothing-here", json={"a": 1})
        assert response.status_code == 404
        assert response.json() == {"detail": "No inbound webhook connector found for slug: nothing-here"}
        response = anonymous.get(BASE + "/whatsapp/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
        assert response.status_code == 403
        assert response.json() == {"detail": "Invalid verify token"}


# =============================================================================
# Discovery: types and events
# =============================================================================


class TestDiscovery:
    def test_types_list_every_connector_type_with_its_fields(self, owner):
        response = owner.get(BASE + "/types")
        assert response.status_code == 200
        types = {t["type"]: t for t in response.json()}
        assert set(types) == {
            "telegram", "discord", "whatsapp", "webhook", "slack", "line", "signal", "imessage",
            "msteams", "bluebubbles", "spotify", "hue", "homeassistant", "gmail", "plaid"}
        telegram = types["telegram"]
        assert telegram["name"] == "Telegram"
        assert telegram["description"] == "Telegram bot via Bot API"
        fields = {f["key"]: f for f in telegram["config_fields"]}
        assert fields["bot_token"] == {"key": "bot_token", "label": "Bot Token", "type": "password", "required": True}
        assert fields["allowed_chat_ids"]["type"] == "list"
        # The allowlist gate is announced on the field that controls it.
        assert fields["allowed_chat_ids"]["description"].startswith("Required to enable this connector")
        assert "(or guild_ids, allowed_channel_ids)" in {
            f["key"]: f for f in types["discord"]["config_fields"]}["guild_ids"]["description"]
        assert telegram["plugin_id"] == "telegram@shuffle-connectors"
        assert {"plugin_installed", "plugin_enabled", "triggers", "actions", "tools"} <= set(telegram)
        assert "inbound_path" in {f["key"] for f in types["webhook"]["config_fields"]}

    def test_events_are_the_valid_connector_events(self, owner):
        response = owner.get(BASE + "/events")
        assert response.status_code == 200
        assert response.json() == VALID_CONNECTOR_EVENTS
        assert {"session.complete", "session.error"} <= set(response.json())


# =============================================================================
# Create: agents, API users and validation
# =============================================================================


class TestCreate:
    def test_new_api_user_gets_the_chosen_agent(self, owner):
        data = create(owner, name="Support Bot", type_="telegram",
                      config={"bot_token": "123456:ABCDEF-secret", "allowed_chat_ids": "111, 222"},
                      profile_ids=["default"])
        assert data["type"] == "telegram" and data["name"] == "Support Bot"
        assert data["is_active"] is False and data["status"] == "disconnected"
        assert data["plugin_id"] == "telegram@shuffle-connectors"
        user = database.get_api_user(data["api_user_id"])
        assert user["name"] == "connector:telegram:Support Bot"
        assert user["profile_ids"] == ["default"]
        assert not user["web_login_allowed"]
        # The secret never comes back and is encrypted at rest.
        assert "ABCDEF-secret" not in json.dumps(data)
        assert data["config"]["bot_token"].startswith("****")
        stored = database.get_connector(data["id"])["config"]
        assert encryption.is_encrypted(stored["bot_token"])
        assert decrypt_config(stored, "telegram")["bot_token"] == "123456:ABCDEF-secret"
        # A comma list typed into a list field is stored as a list.
        assert data["config"]["allowed_chat_ids"] == ["111", "222"]
        assert stored["runtime_plugin_id"] == "telegram@shuffle-connectors"

    def test_without_profile_ids_the_new_api_user_has_no_agent(self, owner):
        data = create(owner, name="Hook")
        user = database.get_api_user(data["api_user_id"])
        assert user["name"] == "connector:webhook:Hook"
        assert not user.get("profile_ids")

    def test_existing_api_user_is_linked_and_keeps_its_own_agents(self, owner):
        database.create_api_user("integration", "Integration", auth.hash_api_key("aih_x"), profile_ids=["default"])
        before = len(database.get_all_api_users())
        data = create(owner, name="Linked", api_user_id="integration", profile_ids=["someone-else"])
        assert data["api_user_id"] == "integration"
        assert len(database.get_all_api_users()) == before
        assert database.get_api_user("integration")["profile_ids"] == ["default"]

    def test_unknown_api_user_is_400_and_leaves_nothing_behind(self, owner):
        before = len(database.get_all_api_users())
        response = owner.post(BASE, json={"name": "Ghost", "type": "webhook", "config": {}, "api_user_id": "ghost"})
        assert response.status_code == 400
        assert response.json() == {"detail": "API user not found: ghost"}
        assert database.get_all_connectors() == []
        assert len(database.get_all_api_users()) == before

    def test_invalid_event_type_is_400(self, owner):
        response = owner.post(BASE, json={"name": "E", "type": "webhook", "events": ["session.complete", "bogus"]})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid event types: bogus"}
        assert database.get_all_connectors() == []

    @pytest.mark.parametrize("body,field", [
        ({"name": "X", "type": "fax"}, "type"),
        ({"type": "webhook"}, "name"),
        ({"name": "", "type": "webhook"}, "name"),
        ({"name": "x" * 101, "type": "webhook"}, "name"),
        ({"name": "X", "type": "webhook", "config": "mode=inbound"}, "config"),
        ({"name": "X", "type": "webhook", "events": "session.complete"}, "events"),
    ], ids=["unknown-type", "missing-name", "empty-name", "long-name", "config-not-object", "events-not-list"])
    def test_malformed_body_is_422_naming_the_field(self, owner, body, field):
        response = owner.post(BASE, json=body)
        assert response.status_code == 422
        assert any(err["loc"][-1] == field or field in err["loc"] for err in response.json()["detail"])
        assert database.get_all_connectors() == []

    def test_app_not_installed_is_400_and_creates_no_api_user(self, make_client, fake_manager):
        client = make_client()  # no bundled catalog: nothing is installed
        before = len(database.get_all_api_users())
        response = client.post(BASE, json={"name": "T", "type": "telegram", "config": {"bot_token": "t"}})
        assert response.status_code == 400
        assert response.json() == {"detail": "Install and enable this app in Plugins before connecting an account"}
        assert len(database.get_all_api_users()) == before

    def test_database_failure_is_a_generic_500(self, owner, monkeypatch):
        monkeypatch.setattr(database, "create_connector", lambda **kw: None)
        before = len(database.get_all_api_users())
        response = owner.post(BASE, json={"name": "T", "type": "webhook", "config": {}})
        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to create connector"}
        # The API user made for it is rolled back.
        assert len(database.get_all_api_users()) == before


# =============================================================================
# Read, update, delete
# =============================================================================


class TestReadUpdateDelete:
    def test_list_and_get_mask_secrets(self, owner):
        cid = create(owner, name="Tg", type_="telegram", config={"bot_token": "123:secret-token"})["id"]
        listed = owner.get(BASE).json()
        assert [c["id"] for c in listed] == [cid]
        got = owner.get(f"{BASE}/{cid}")
        assert got.status_code == 200
        for body in (listed[0], got.json()):
            assert body["config"]["bot_token"].startswith("****")
            assert "secret-token" not in json.dumps(body)

    def test_docs_describe_the_mask_the_api_returns(self, owner):
        # Stored secrets are encrypted, so the mask is a bare "****", not the last 4 characters.
        cid = create(owner, name="Tg", type_="telegram", config={"bot_token": "123:secret-token"})["id"]
        assert owner.get(f"{BASE}/{cid}").json()["config"]["bot_token"] == "****"
        from pathlib import Path
        page = (Path(__file__).resolve().parents[2] / "docs/content/api-reference/connectors.md").read_text()
        assert "last 4 characters" not in page
        assert "returned as `****`" in page
        assert '"bot_token": "****",' in page

    def test_get_unknown_is_404(self, owner):
        response = owner.get(BASE + "/conn-missing")
        assert response.status_code == 404
        assert response.json() == {"detail": "Connector not found: conn-missing"}

    def test_rename_does_not_touch_the_live_client(self, owner, fake_manager):
        cid = create(owner, name="Old")["id"]
        response = owner.patch(f"{BASE}/{cid}", json={"name": "New"})
        assert response.status_code == 200
        assert response.json()["name"] == "New"
        assert fake_manager.calls == []

    def test_config_change_restarts_an_active_client(self, owner, fake_manager):
        cid = create(owner, name="Tg", type_="telegram",
                     config={"bot_token": "123:secret-token", "allowed_chat_ids": ["1"]})["id"]
        assert owner.post(f"{BASE}/{cid}/activate").status_code == 200
        fake_manager.calls.clear()
        # The UI sends the masked token back; it must not overwrite the real one.
        response = owner.patch(f"{BASE}/{cid}", json={"config": {"bot_token": "****", "allowed_chat_ids": ["1", "2"]}})
        assert response.status_code == 200
        assert response.json()["config"]["allowed_chat_ids"] == ["1", "2"]
        assert fake_manager.calls == [("restart", cid)]
        stored = decrypt_config(database.get_connector(cid)["config"], "telegram")
        assert stored["bot_token"] == "123:secret-token"
        assert stored["runtime_plugin_id"] == "telegram@shuffle-connectors"

    def test_unchanged_config_or_inactive_connector_is_not_restarted(self, owner, fake_manager):
        config = {"mode": "inbound", "inbound_path": "hook", "inbound_token": "hook-token"}
        cid = create(owner, name="Hook", config=config)["id"]
        # Inactive: a real change is saved but nothing is restarted.
        changed = {**config, "mode": "bidirectional", "inbound_token": "****"}
        assert owner.patch(f"{BASE}/{cid}", json={"config": changed}).status_code == 200
        assert fake_manager.calls == []
        owner.post(f"{BASE}/{cid}/activate")
        fake_manager.calls.clear()
        # Active, but the form sent back what is already stored (secret masked).
        assert owner.patch(f"{BASE}/{cid}", json={"config": changed}).status_code == 200
        assert fake_manager.calls == []
        assert decrypt_config(database.get_connector(cid)["config"], "webhook")["inbound_token"] == "hook-token"

    def test_partial_config_patch_keeps_the_stored_secret(self, owner):
        # The docs' PATCH example sends only allowed_chat_ids.
        cid = create(owner, name="Tg", type_="telegram",
                     config={"bot_token": "123:secret-token", "allowed_chat_ids": ["1"]})["id"]
        response = owner.patch(f"{BASE}/{cid}", json={"name": "Renamed Bot", "config": {"allowed_chat_ids": ["12345678"]}})
        assert response.status_code == 200
        assert response.json()["config"]["bot_token"] == "****"
        assert response.json()["config"]["allowed_chat_ids"] == ["12345678"]
        stored = decrypt_config(database.get_connector(cid)["config"], "telegram")
        assert stored.get("bot_token") == "123:secret-token"
        assert stored["allowed_chat_ids"] == ["12345678"]
        assert stored["runtime_plugin_id"] == "telegram@shuffle-connectors"

    def test_partial_config_patch_restarts_with_the_merged_config(self, owner, fake_manager):
        cid = create(owner, name="Tg", type_="telegram",
                     config={"bot_token": "123:secret-token", "allowed_chat_ids": ["1"]})["id"]
        assert owner.post(f"{BASE}/{cid}/activate").status_code == 200
        fake_manager.calls.clear()
        seen = []

        async def restart(connector_id):
            fake_manager.calls.append(("restart", connector_id))
            seen.append(decrypt_config(database.get_connector(connector_id)["config"], "telegram"))
            return True

        fake_manager.restart_connector = restart
        response = owner.patch(f"{BASE}/{cid}", json={"config": {"allowed_chat_ids": "7, 8"}})
        assert response.status_code == 200
        assert fake_manager.calls == [("restart", cid)]
        assert seen[0]["bot_token"] == "123:secret-token"
        assert seen[0]["allowed_chat_ids"] == ["7", "8"]
        # Sending the stored values again is no change: no second restart.
        fake_manager.calls.clear()
        assert owner.patch(f"{BASE}/{cid}", json={"config": {"allowed_chat_ids": ["7", "8"]}}).status_code == 200
        assert fake_manager.calls == []

    def test_owner_sender_ids_persist_without_restarting_the_client(self, owner, fake_manager):
        cid = create(owner, name="Tg", type_="telegram",
                     config={"bot_token": "123:secret-token", "allowed_chat_ids": ["1"]})["id"]
        assert owner.post(f"{BASE}/{cid}/activate").status_code == 200
        fake_manager.calls.clear()
        response = owner.patch(f"{BASE}/{cid}", json={"config": {"owner_sender_ids": "111, 222 111"}})
        assert response.status_code == 200, response.text
        assert response.json()["config"]["owner_sender_ids"] == ["111", "222"]
        # Who counts as the owner is read per message; the bot needs no restart.
        assert fake_manager.calls == []
        assert owner.get(f"{BASE}/{cid}").json()["config"]["owner_sender_ids"] == ["111", "222"]
        stored = decrypt_config(database.get_connector(cid)["config"], "telegram")
        assert stored["owner_sender_ids"] == ["111", "222"]
        assert stored["bot_token"] == "123:secret-token"
        # Clearing it is saved too.
        assert owner.patch(f"{BASE}/{cid}", json={"config": {"owner_sender_ids": []}}).status_code == 200
        assert database.get_connector(cid)["config"]["owner_sender_ids"] == []

    def test_owner_sender_ids_are_refused_where_no_sender_can_be_recognised(self, owner):
        cid = create(owner, name="Hook")["id"]
        response = owner.patch(f"{BASE}/{cid}", json={"config": {"owner_sender_ids": ["111"]}})
        assert response.status_code == 400
        assert "can't tell which messages are yours" in response.json()["detail"]
        assert "owner_sender_ids" not in database.get_connector(cid)["config"]

    def test_types_say_which_connectors_know_the_owners_account(self, owner):
        types = {t["type"]: t for t in owner.get(f"{BASE}/types").json()}
        assert types["telegram"]["owner_sender"] is True
        assert types["webhook"]["owner_sender"] is False

    def test_senders_lists_who_messaged_the_connection(self, owner):
        cid = create(owner, name="Tg", type_="telegram",
                     config={"bot_token": "123:secret-token", "allowed_chat_ids": ["1"]})["id"]
        assert owner.get(f"{BASE}/{cid}/senders").json() == []
        database.create_connector_session("cs-1", cid, "chat-1", "111", "Owner")
        senders = owner.get(f"{BASE}/{cid}/senders").json()
        assert [(s["platform_user_id"], s["platform_user_name"]) for s in senders] == [("111", "Owner")]
        assert owner.get(f"{BASE}/nope/senders").status_code == 404

    def test_disable_through_patch_stops_the_live_client(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        owner.post(f"{BASE}/{cid}/activate")
        assert cid in fake_manager.running
        fake_manager.calls.clear()
        response = owner.patch(f"{BASE}/{cid}", json={"is_active": False})
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert fake_manager.calls == [("stop", cid)]
        assert cid not in fake_manager.running

    def test_enable_through_patch_starts_the_live_client(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        response = owner.patch(f"{BASE}/{cid}", json={"is_active": True})
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        assert fake_manager.calls == [("start", cid)]

    def test_patch_on_another_install_does_not_touch_a_local_client(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        set_runner(cid, "other-install")
        response = owner.patch(f"{BASE}/{cid}", json={"is_active": True})
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        assert fake_manager.calls == []

    def test_patch_validation(self, owner):
        cid = create(owner, name="Hook")["id"]
        bad_events = owner.patch(f"{BASE}/{cid}", json={"events": ["nope"]})
        assert bad_events.status_code == 400
        assert bad_events.json() == {"detail": "Invalid event types: nope"}
        assert owner.patch(f"{BASE}/{cid}", json={"name": ""}).status_code == 422
        missing = owner.patch(BASE + "/conn-missing", json={"name": "x"})
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Connector not found: conn-missing"}

    def test_disabling_a_connection_an_enabled_automation_uses_is_refused(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        owner.post(f"{BASE}/{cid}/activate")
        add_automation("auto-1", cid, name="Morning digest")
        fake_manager.calls.clear()
        response = owner.patch(f"{BASE}/{cid}", json={"is_active": False})
        assert response.status_code == 400
        assert "Morning digest" in response.json()["detail"]
        assert fake_manager.calls == []
        assert database.get_connector(cid)["is_active"]

    def test_delete_stops_the_client_and_removes_its_own_api_user(self, owner, fake_manager):
        data = create(owner, name="Hook")
        owner.post(f"{BASE}/{data['id']}/activate")
        fake_manager.calls.clear()
        response = owner.delete(f"{BASE}/{data['id']}")
        assert response.status_code == 204
        assert response.content == b""
        assert fake_manager.calls == [("stop", data["id"])]
        assert database.get_connector(data["id"]) is None
        assert database.get_api_user(data["api_user_id"]) is None
        assert owner.get(f"{BASE}/{data['id']}").status_code == 404

    def test_delete_keeps_a_linked_api_user(self, owner):
        database.create_api_user("integration", "Integration", auth.hash_api_key("aih_x"))
        cid = create(owner, name="Linked", api_user_id="integration")["id"]
        assert owner.delete(f"{BASE}/{cid}").status_code == 204
        assert database.get_api_user("integration") is not None

    def test_delete_is_refused_while_any_automation_uses_it(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        add_automation("auto-off", cid, enabled=False, name="Paused digest")
        response = owner.delete(f"{BASE}/{cid}")
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "This connection is used by automations: Paused digest. Update those automations first.")
        assert database.get_connector(cid) is not None
        assert fake_manager.calls == []

    def test_delete_unknown_is_404(self, owner):
        response = owner.delete(BASE + "/conn-missing")
        assert response.status_code == 404
        assert response.json() == {"detail": "Connector not found: conn-missing"}


# =============================================================================
# Lifecycle: test, activate, deactivate, restart
# =============================================================================


class TestLifecycle:
    def test_activate_enables_and_starts(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        response = owner.post(f"{BASE}/{cid}/activate")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == cid and body["is_active"] is True
        assert body["applies_on"] is None
        assert body["config"]["inbound_token"].startswith("****")
        assert fake_manager.calls == [("start", cid)]

    def test_activate_failure_is_500_with_the_client_error(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        fake_manager.start_ok = False
        fake_manager.start_error = "bad credentials"
        response = owner.post(f"{BASE}/{cid}/activate")
        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to start connector: bad credentials"}

    def test_activate_chat_connector_without_allowlist_is_400(self, make_client, bundled_connector_catalog):
        # The real manager: its allowlist gate runs before any client is built.
        client = make_client()
        cid = create(client, name="Tg", type_="telegram", config={"bot_token": "123:token"})["id"]
        response = client.post(f"{BASE}/{cid}/activate")
        assert response.status_code == 400
        assert response.json() == {"detail": ALLOWLIST_REQUIRED_MESSAGE}
        row = database.get_connector(cid)
        assert row["status"] == "error" and row["last_error"] == ALLOWLIST_REQUIRED_MESSAGE
        from app.core.connectors.manager import connector_manager
        assert connector_manager.get_connector_status(cid) is None

    def test_activate_on_another_install_only_persists_the_flag(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        set_runner(cid, "other-install")
        response = owner.post(f"{BASE}/{cid}/activate")
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        assert response.json()["applies_on"] == "other-install"
        assert fake_manager.calls == []

    def test_unowned_connector_is_claimed_then_started(self, owner, fake_manager):
        from app.core.config import settings
        cid = create(owner, name="Hook")["id"]
        set_runner(cid, None)
        assert owner.post(f"{BASE}/{cid}/activate").status_code == 200
        assert database.get_connector(cid)["runner_id"] == settings.effective_instance_id
        assert fake_manager.calls == [("start", cid)]

    def test_deactivate_stops_and_disables(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        owner.post(f"{BASE}/{cid}/activate")
        fake_manager.calls.clear()
        response = owner.post(f"{BASE}/{cid}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert response.json()["applies_on"] is None
        assert fake_manager.calls == [("stop", cid)]

    def test_deactivate_is_409_while_an_enabled_automation_uses_it(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        owner.post(f"{BASE}/{cid}/activate")
        add_automation("auto-1", cid, name="Inbox triage")
        fake_manager.calls.clear()
        response = owner.post(f"{BASE}/{cid}/deactivate")
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "This connection is used by automations: Inbox triage. Update those automations first.")
        assert fake_manager.calls == []
        assert database.get_connector(cid)["is_active"]

    def test_deactivate_ignores_disabled_automations(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        add_automation("auto-off", cid, enabled=False)
        assert owner.post(f"{BASE}/{cid}/deactivate").status_code == 200

    def test_deactivate_on_another_install_only_persists_the_flag(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        owner.post(f"{BASE}/{cid}/activate")
        set_runner(cid, "other-install")
        fake_manager.calls.clear()
        response = owner.post(f"{BASE}/{cid}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert response.json()["applies_on"] == "other-install"
        assert fake_manager.calls == []

    def test_restart(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        response = owner.post(f"{BASE}/{cid}/restart")
        assert response.status_code == 200
        assert response.json()["id"] == cid and response.json()["applies_on"] is None
        assert fake_manager.calls == [("restart", cid)]
        set_runner(cid, "other-install")
        fake_manager.calls.clear()
        response = owner.post(f"{BASE}/{cid}/restart")
        assert response.json()["applies_on"] == "other-install"
        assert fake_manager.calls == []

    @pytest.mark.parametrize("action", ["test", "activate", "deactivate", "restart"])
    def test_lifecycle_on_unknown_connector_is_404(self, owner, fake_manager, action):
        response = owner.post(f"{BASE}/conn-missing/{action}")
        assert response.status_code == 404
        assert response.json() == {"detail": "Connector not found: conn-missing"}
        assert fake_manager.calls == []

    def test_test_endpoint_uses_decrypted_config_without_starting(self, owner, fake_manager, monkeypatch):
        seen = {}

        class Probe:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            async def test_connection(self):
                return {"success": True, "message": "Connection successful"}

        from app.core.connectors import registry
        monkeypatch.setattr(registry, "get_connector_class", lambda kind: Probe)
        cid = create(owner, name="Tg", type_="telegram", config={"bot_token": "123:secret"})["id"]
        response = owner.post(f"{BASE}/{cid}/test")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True and body["message"] == "Connection successful"
        assert isinstance(body["response_time_ms"], int)
        assert seen["config"]["bot_token"] == "123:secret"
        assert fake_manager.calls == []


# =============================================================================
# Status, logs, dependencies and the type catalog routes
# =============================================================================


class TestInspection:
    def test_status_reports_whether_the_client_runs(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        body = owner.get(f"{BASE}/{cid}/status").json()
        assert body == {"id": cid, "status": "disconnected", "status_message": None, "last_connected_at": None,
                        "last_event_at": None, "failure_count": 0, "is_running": False}
        owner.post(f"{BASE}/{cid}/activate")
        assert owner.get(f"{BASE}/{cid}/status").json()["is_running"] is True

    def test_logs_are_the_newest_fifty_for_this_connector(self, owner):
        cid = create(owner, name="Hook")["id"]
        other = create(owner, name="Other")["id"]
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with get_db() as conn:
            cursor = conn.cursor()
            for i in range(55):
                cursor.execute(
                    "INSERT INTO connector_event_log (id, connector_id, event_type, direction, payload, status, error, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (f"evt-{i:02d}", cid, "message_received", "inbound", json.dumps({"n": i}),
                     "ok" if i % 2 else "error", None if i % 2 else "failed", (start + timedelta(minutes=i)).isoformat()))
            cursor.execute(
                "INSERT INTO connector_event_log (id, connector_id, event_type, direction, payload, status, error, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                ("evt-other", other, "message_received", "inbound", None, "ok", None, start.isoformat()))
        response = owner.get(f"{BASE}/{cid}/logs")
        assert response.status_code == 200
        logs = response.json()
        assert [e["id"] for e in logs] == [f"evt-{i:02d}" for i in range(54, 4, -1)]
        newest = logs[0]
        assert newest["connector_id"] == cid and newest["event_type"] == "message_received"
        assert newest["direction"] == "inbound" and newest["payload"] == {"n": 54}
        assert newest["status"] == "error" and newest["error"] == "failed"
        assert newest["created_at"].startswith("2025-01-01T00:54")
        assert owner.get(f"{BASE}/{other}/logs").json()[0]["payload"] is None

    def test_logs_for_a_new_connector_are_empty_and_unknown_is_404(self, owner):
        cid = create(owner, name="Hook")["id"]
        assert owner.get(f"{BASE}/{cid}/logs").json() == []
        response = owner.get(BASE + "/conn-missing/logs")
        assert response.status_code == 404
        assert response.json() == {"detail": "Connector not found: conn-missing"}

    def test_dependencies_name_where_the_connection_is_used(self, owner):
        cid = create(owner, name="Hook")["id"]
        other = create(owner, name="Other")["id"]
        add_automation("a-trigger", cid, name="Uses trigger", output=True)
        add_automation("a-any", None, name="Any connection", enabled=False)
        add_automation("a-other", other, name="Other only")
        response = owner.get(f"{BASE}/{cid}/dependencies")
        assert response.status_code == 200
        assert sorted(response.json(), key=lambda d: d["id"]) == [
            {"id": "a-any", "name": "Any connection", "is_enabled": False, "uses": ["trigger"]},
            {"id": "a-trigger", "name": "Uses trigger", "is_enabled": True, "uses": ["output", "trigger"]},
        ]
        missing = owner.get(BASE + "/conn-missing/dependencies")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Connection not found"}

    @pytest.mark.parametrize("route,attr", [("triggers", "TRIGGERS"), ("actions", "ACTIONS"), ("tools", "TOOLS")])
    def test_catalog_routes_list_the_type_definitions(self, owner, route, attr):
        from dataclasses import asdict
        from app.core.connectors.registry import get_connector_class
        cid = create(owner, name="Hook")["id"]
        response = owner.get(f"{BASE}/{cid}/{route}")
        assert response.status_code == 200
        assert response.json() == [asdict(d) for d in getattr(get_connector_class("webhook"), attr)]
        missing = owner.get(f"{BASE}/conn-missing/{route}")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Connector not found: conn-missing"}

    @pytest.mark.parametrize("route", ["triggers", "actions", "tools", "actions/send_webhook/execute"])
    def test_catalog_routes_refuse_a_retired_type(self, owner, route):
        cid = create(owner, name="Hook")["id"]
        with get_db() as conn:
            conn.cursor().execute("UPDATE connectors SET type = %s WHERE id = %s", ("fax", cid))
        method = owner.post if route.endswith("execute") else owner.get
        response = method(f"{BASE}/{cid}/{route}")
        assert response.status_code == 400
        assert response.json() == {"detail": "Unknown connector type: fax"}


class TestExecuteAction:
    def _running(self, owner, fake_manager, **behaviour):
        cid = create(owner, name="Hook")["id"]
        instance = SimpleNamespace(connector_type="webhook", execute_action=AsyncMock(**behaviour))
        fake_manager.running[cid] = instance
        return cid, instance

    def test_body_is_passed_as_parameters(self, owner, fake_manager):
        cid, instance = self._running(owner, fake_manager, return_value={"delivered": True})
        response = owner.post(f"{BASE}/{cid}/actions/send_webhook/execute", json={"payload": {"a": 1}})
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "action_id": "send_webhook", "result": {"delivered": True}}
        instance.execute_action.assert_awaited_once_with("send_webhook", {"payload": {"a": 1}})

    def test_no_body_means_no_parameters(self, owner, fake_manager):
        cid, instance = self._running(owner, fake_manager, return_value=None)
        response = owner.post(f"{BASE}/{cid}/actions/send_webhook/execute", content=b"not json")
        assert response.status_code == 200
        instance.execute_action.assert_awaited_once_with("send_webhook", {})

    def test_unknown_action_is_400(self, owner, fake_manager):
        cid, instance = self._running(owner, fake_manager)
        response = owner.post(f"{BASE}/{cid}/actions/transfer_money/execute", json={})
        assert response.status_code == 400
        assert response.json()["detail"].startswith("Unknown action 'transfer_money' for connector type 'webhook'")
        instance.execute_action.assert_not_awaited()

    def test_not_running_is_409(self, owner, fake_manager):
        cid = create(owner, name="Hook")["id"]
        response = owner.post(f"{BASE}/{cid}/actions/send_webhook/execute", json={})
        assert response.status_code == 409
        assert response.json() == {"detail": f"Connector '{cid}' is not running. Activate it first."}

    def test_unknown_connector_is_404(self, owner):
        response = owner.post(BASE + "/conn-missing/actions/send_webhook/execute", json={})
        assert response.status_code == 404

    def test_unimplemented_action_is_501(self, owner, fake_manager):
        cid, _ = self._running(owner, fake_manager, side_effect=NotImplementedError("not on this platform"))
        response = owner.post(f"{BASE}/{cid}/actions/send_webhook/execute", json={})
        assert response.status_code == 501
        assert response.json() == {"detail": "not on this platform"}

    def test_action_crash_is_a_generic_500(self, owner, fake_manager):
        cid, _ = self._running(owner, fake_manager, side_effect=RuntimeError("token=hunter2"))
        response = owner.post(f"{BASE}/{cid}/actions/send_webhook/execute", json={})
        assert response.status_code == 500
        assert response.json() == {"detail": f"Failed to execute action 'send_webhook' on connector '{cid}'"}


# =============================================================================
# Discord guided setup (cases the older suite does not cover)
# =============================================================================


class TestDiscordDiscover:
    def test_stored_connector_without_a_token_is_400(self, owner, monkeypatch):
        discover = AsyncMock()
        monkeypatch.setattr("app.core.connectors.discord_connector.discover_resources", discover)
        cid = create(owner, name="Disco", type_="discord", config={"guild_ids": ["1"]})["id"]
        response = owner.post(BASE + "/discord/discover", json={"connector_id": cid})
        assert response.status_code == 400
        assert response.json() == {"detail": "Connector has no stored bot token"}
        discover.assert_not_awaited()

    def test_upstream_error_is_502_and_never_echoes_the_token(self, owner, monkeypatch):
        monkeypatch.setattr("app.core.connectors.discord_connector.discover_resources",
                            AsyncMock(side_effect=RuntimeError("401 for token very-secret-token")))
        response = owner.post(BASE + "/discord/discover", json={"bot_token": "very-secret-token"})
        assert response.status_code == 502
        assert response.json() == {"detail": "Discord discovery failed"}


# =============================================================================
# Public webhooks
# =============================================================================


def whatsapp_payload(phone_number_id="555"):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}}}]}]}


def meta_signature(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestWhatsAppWebhook:
    URL = BASE + "/whatsapp/webhook"

    def _connector(self, owner, fake_manager, name="Wa", app_secret="meta-secret", phone="555"):
        config = {"phone_number_id": phone, "access_token": "EAAG-token", "verify_token": "verify-me",
                  "allowed_phone_numbers": ["+15550001"]}
        if app_secret:
            config["app_secret"] = app_secret
        cid = create(owner, name=name, type_="whatsapp", config=config)["id"]
        instance = SimpleNamespace(connector_type="whatsapp", handle_webhook_payload=AsyncMock())
        fake_manager.running[cid] = instance
        return instance

    def test_verification_echoes_the_challenge_for_a_known_token(self, owner, fake_manager):
        self._connector(owner, fake_manager)
        response = owner.get(self.URL, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                               "hub.challenge": "1158201444"})
        assert response.status_code == 200
        assert response.text == "1158201444"
        assert response.headers["content-type"].startswith("text/plain")

    def test_verification_with_an_unknown_token_is_403(self, owner, fake_manager):
        self._connector(owner, fake_manager)
        response = owner.get(self.URL, params={"hub.mode": "subscribe", "hub.verify_token": "guess",
                                               "hub.challenge": "1"})
        assert response.status_code == 403
        assert response.json() == {"detail": "Invalid verify token"}

    def test_signed_payload_is_dispatched(self, owner, fake_manager):
        instance = self._connector(owner, fake_manager)
        body = json.dumps(whatsapp_payload()).encode()
        response = owner.post(self.URL, content=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": meta_signature("meta-secret", body)})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        instance.handle_webhook_payload.assert_awaited_once_with(whatsapp_payload())

    @pytest.mark.parametrize("signature", [None, "sha256=deadbeef", "wrong-secret"])
    def test_unsigned_or_wrongly_signed_payload_is_403(self, owner, fake_manager, signature):
        instance = self._connector(owner, fake_manager)
        body = json.dumps(whatsapp_payload()).encode()
        headers = {"Content-Type": "application/json"}
        if signature == "wrong-secret":
            headers["X-Hub-Signature-256"] = meta_signature("not-the-secret", body)
        elif signature:
            headers["X-Hub-Signature-256"] = signature
        response = owner.post(self.URL, content=body, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Invalid signature"}
        instance.handle_webhook_payload.assert_not_awaited()

    def test_connector_without_app_secret_accepts_nothing(self, owner, fake_manager):
        instance = self._connector(owner, fake_manager, app_secret=None)
        body = json.dumps(whatsapp_payload()).encode()
        response = owner.post(self.URL, content=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": meta_signature("", body)})
        assert response.status_code == 403
        instance.handle_webhook_payload.assert_not_awaited()

    def test_payload_goes_only_to_the_matching_phone_number(self, owner, fake_manager):
        mine = self._connector(owner, fake_manager, name="Mine", phone="555")
        theirs = self._connector(owner, fake_manager, name="Theirs", phone="777")
        body = json.dumps(whatsapp_payload("777")).encode()
        response = owner.post(self.URL, content=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": meta_signature("meta-secret", body)})
        assert response.status_code == 200
        theirs.handle_webhook_payload.assert_awaited_once()
        mine.handle_webhook_payload.assert_not_awaited()

    def test_handler_failure_is_500(self, owner, fake_manager):
        instance = self._connector(owner, fake_manager)
        instance.handle_webhook_payload.side_effect = RuntimeError("boom")
        body = json.dumps(whatsapp_payload()).encode()
        response = owner.post(self.URL, content=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": meta_signature("meta-secret", body)})
        assert response.status_code == 500
        assert response.json() == {"detail": "Webhook processing failed"}

    def test_invalid_json_is_400(self, owner):
        response = owner.post(self.URL, content=b"{", headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid JSON"}


class TestInboundWebhook:
    def _hook(self, owner, fake_manager, slug="deploys", **config):
        from app.core.connectors.webhook_connector import WebhookConnector
        config = {"mode": "inbound", "inbound_path": slug, **config}
        cid = create(owner, name=slug, config=config)["id"]
        instance = WebhookConnector(connector_id=cid, connector_type="webhook", config=config,
                                    on_incoming_message=AsyncMock(), on_permission_response=AsyncMock())
        instance.handle_webhook_payload = AsyncMock()
        fake_manager.running[cid] = instance
        return instance

    @pytest.mark.parametrize("header", ["Authorization", "X-Webhook-Token"])
    def test_token_authenticated_payload_is_dispatched(self, owner, fake_manager, header):
        hook = self._hook(owner, fake_manager, inbound_token="hook-token")
        value = "Bearer hook-token" if header == "Authorization" else "hook-token"
        response = owner.post(BASE + "/hooks/deploys", json={"message": "hi"}, headers={header: value})
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "message": "Webhook received and processing"}
        hook.handle_webhook_payload.assert_awaited_once_with({"message": "hi"})

    @pytest.mark.parametrize("prefix", ["", "sha256="])
    def test_hmac_signed_payload_is_dispatched(self, owner, fake_manager, prefix):
        hook = self._hook(owner, fake_manager, secret="hmac-secret")
        body = b'{"message": "signed"}'
        signature = prefix + hmac.new(b"hmac-secret", body, hashlib.sha256).hexdigest()
        response = owner.post(BASE + "/hooks/deploys", content=body, headers={
            "Content-Type": "application/json", "X-Webhook-Signature": signature})
        assert response.status_code == 200
        hook.handle_webhook_payload.assert_awaited_once_with({"message": "signed"})

    @pytest.mark.parametrize("config,headers", [
        ({"inbound_token": "hook-token"}, {"Authorization": "Bearer wrong"}),
        ({"inbound_token": "hook-token"}, {}),
        ({"secret": "hmac-secret"}, {"X-Webhook-Signature": "sha256=" + "0" * 64}),
        ({"secret": "hmac-secret"}, {}),
        ({}, {"Authorization": "Bearer anything"}),
    ], ids=["wrong-token", "no-token", "wrong-signature", "no-signature", "no-credentials-configured"])
    def test_bad_or_missing_credentials_are_403(self, owner, fake_manager, config, headers):
        hook = self._hook(owner, fake_manager, **config)
        response = owner.post(BASE + "/hooks/deploys", json={"x": 1}, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Invalid authentication"}
        hook.handle_webhook_payload.assert_not_awaited()

    def test_signature_from_another_secret_is_403(self, owner, fake_manager):
        hook = self._hook(owner, fake_manager, secret="hmac-secret")
        body = b'{"x": 1}'
        signature = hmac.new(b"other-secret", body, hashlib.sha256).hexdigest()
        response = owner.post(BASE + "/hooks/deploys", content=body, headers={
            "Content-Type": "application/json", "X-Webhook-Signature": signature})
        assert response.status_code == 403
        hook.handle_webhook_payload.assert_not_awaited()

    def test_unknown_slug_and_outbound_only_hooks_are_404(self, owner, fake_manager):
        outbound = self._hook(owner, fake_manager, slug="outbound", mode="outbound", inbound_token="t")
        for slug in ("nope", "outbound"):
            response = owner.post(f"{BASE}/hooks/{slug}", json={}, headers={"Authorization": "Bearer t"})
            assert response.status_code == 404
            assert response.json() == {"detail": f"No inbound webhook connector found for slug: {slug}"}
        outbound.handle_webhook_payload.assert_not_awaited()

    def test_invalid_json_is_400(self, owner, fake_manager):
        self._hook(owner, fake_manager, inbound_token="hook-token")
        response = owner.post(BASE + "/hooks/deploys", content=b"not json", headers={
            "Content-Type": "application/json", "Authorization": "Bearer hook-token"})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid JSON body"}

    def test_oversized_body_is_413_before_the_handler(self, owner, fake_manager, monkeypatch):
        from app.core.config import settings
        hook = self._hook(owner, fake_manager, inbound_token="hook-token")
        monkeypatch.setattr(settings, "max_request_body_mb", 1)
        body = json.dumps({"blob": "x" * (1024 * 1024)}).encode()
        response = owner.post(BASE + "/hooks/deploys", content=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer hook-token"})
        assert response.status_code == 413
        assert response.json() == {"detail": "Request body too large. Maximum size is 1MB"}
        hook.handle_webhook_payload.assert_not_awaited()

    def test_handler_failure_is_a_generic_500(self, owner, fake_manager):
        hook = self._hook(owner, fake_manager, inbound_token="hook-token")
        hook.handle_webhook_payload.side_effect = RuntimeError("secret detail")
        response = owner.post(BASE + "/hooks/deploys", json={}, headers={"Authorization": "Bearer hook-token"})
        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to process inbound webhook"}

    @pytest.mark.parametrize("slug", ["test", "activate", "deactivate", "restart"])
    def test_slug_that_matches_an_action_name_still_reaches_the_hook(self, make_client, bundled_connector_catalog,
                                                                      fake_manager, slug):
        # Regression: /hooks/{slug} was declared after /{connector_id}/{action},
        # so POST /hooks/test was routed to the owner-only "test" action for a
        # connector named "hooks" and the external caller got 403.
        owner = make_client()
        hook = self._hook(owner, fake_manager, slug=slug, inbound_token="hook-token")
        from app.main import app
        app.dependency_overrides.pop(auth.require_admin, None)
        public = make_client(owner=False)
        response = public.post(f"{BASE}/hooks/{slug}", json={"n": 1}, headers={"Authorization": "Bearer hook-token"})
        assert response.status_code == 200, response.text
        hook.handle_webhook_payload.assert_awaited_once_with({"n": 1})


# =============================================================================
# Gmail sign-in (Google OAuth)
# =============================================================================


GOOGLE = BASE + "/oauth/google"
LOCAL_CALLBACK = "http://localhost" + google.CALLBACK


@pytest.fixture
def local_owner(make_client, bundled_connector_catalog, fake_manager):
    return make_client(base_url="http://localhost")


def configure_google(client, redirect_uri=LOCAL_CALLBACK):
    return client.put(GOOGLE + "/app", json={"client_id": "test.apps.googleusercontent.com",
                                             "client_secret": "private-client-secret",
                                             "redirect_uri": redirect_uri})


def google_start(client, **body):
    assert configure_google(client).status_code == 200
    response = client.post(GOOGLE + "/start", json=body)
    assert response.status_code == 200, response.text
    return response


GMAIL_CREDENTIALS = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh-secret",
                     "email_address": "person@example.com", "oauth_managed": True}


class TestGoogleOAuth:
    def test_status_before_configuration(self, local_owner):
        response = local_owner.get(GOOGLE + "/status")
        assert response.status_code == 200
        assert response.json() == {"configured": False, "client_id": "", "redirect_uri": "",
                                   "suggested_redirect_uri": LOCAL_CALLBACK, "environment_managed": False}

    def test_configure_then_status_never_returns_the_secret(self, local_owner):
        response = configure_google(local_owner)
        assert response.status_code == 200
        assert response.json() == {"configured": True}
        body = local_owner.get(GOOGLE + "/status").json()
        assert body["configured"] is True and body["client_id"] == "test.apps.googleusercontent.com"
        assert body["redirect_uri"] == LOCAL_CALLBACK
        assert "private-client-secret" not in json.dumps(body)
        assert "private-client-secret" not in database.get_system_setting(google.SETTING)

    @pytest.mark.parametrize("redirect_uri", [
        "https://evil.example" + google.CALLBACK,
        "http://localhost/api/v1/connectors/oauth/google/other",
        LOCAL_CALLBACK + "?next=/admin",
        LOCAL_CALLBACK + "#frag",
        "http://user:pw@localhost" + google.CALLBACK,
        "http://127.0.0.1" + google.CALLBACK,
        "javascript:alert(1)",
    ], ids=["other-origin", "other-path", "query", "fragment", "userinfo", "other-loopback-origin", "js-scheme"])
    def test_redirect_uri_outside_the_allowlist_is_400(self, local_owner, redirect_uri):
        response = configure_google(local_owner, redirect_uri)
        assert response.status_code == 400
        assert response.json()["detail"].startswith("Use this app's HTTPS origin (or localhost)")
        assert database.get_system_setting(google.SETTING) is None

    def test_plain_http_is_refused_off_localhost(self, make_client, bundled_connector_catalog):
        client = make_client(base_url="http://shuffle.example")
        response = configure_google(client, "http://shuffle.example" + google.CALLBACK)
        assert response.status_code == 400
        https = make_client(base_url="https://shuffle.example")
        assert configure_google(https, "https://shuffle.example" + google.CALLBACK).status_code == 200

    def test_environment_managed_settings_cannot_be_changed(self, local_owner, monkeypatch):
        monkeypatch.setenv("SHUFFLE_GOOGLE_CLIENT_ID", "env-client")
        monkeypatch.setenv("SHUFFLE_GOOGLE_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("SHUFFLE_GOOGLE_REDIRECT_URI", LOCAL_CALLBACK)
        status = local_owner.get(GOOGLE + "/status").json()
        assert status["configured"] is True and status["environment_managed"] is True
        assert status["client_id"] == "env-client" and "env-secret" not in json.dumps(status)
        response = configure_google(local_owner)
        assert response.status_code == 400
        assert response.json() == {"detail": "Google app settings are managed by this host's environment"}

    def test_locked_credential_storage_is_409(self, local_owner, monkeypatch):
        def locked(_value):
            raise RuntimeError("key unavailable")
        monkeypatch.setattr(encryption, "encrypt_value", locked)
        response = configure_google(local_owner)
        assert response.status_code == 409
        assert response.json()["detail"].startswith("Credential storage is locked")

    def test_start_before_configuration_is_400(self, local_owner):
        response = local_owner.post(GOOGLE + "/start", json={})
        assert response.status_code == 400
        assert response.json() == {
            "detail": "An administrator must configure a Google OAuth app for this installation first"}

    def test_start_from_another_origin_is_400(self, local_owner, make_client):
        assert configure_google(local_owner).status_code == 200
        elsewhere = make_client(base_url="http://127.0.0.1")
        response = elsewhere.post(GOOGLE + "/start", json={})
        assert response.status_code == 400
        assert google._pending == {}

    def test_start_issues_pkce_url_and_a_callback_scoped_cookie(self, local_owner):
        response = google_start(local_owner, name="Work mail")
        body = response.json()
        assert set(body) == {"authorization_url", "flow_id"}
        query = parse_qs(urlsplit(body["authorization_url"]).query)
        assert body["authorization_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert query["state"] == [body["flow_id"]]
        assert query["redirect_uri"] == [LOCAL_CALLBACK]
        assert query["code_challenge_method"] == ["S256"] and query["access_type"] == ["offline"]
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("gmail_oauth_" + body["flow_id"][:16] + "=")
        assert "HttpOnly" in cookie and f"Path={google.CALLBACK}" in cookie
        assert "Max-Age=600" in cookie and "samesite=lax" in cookie.lower()
        assert "no-store" in response.headers["cache-control"]

    @pytest.mark.parametrize("case", ["missing", "wrong-type", "other-install"])
    def test_start_reconnect_validates_the_target(self, local_owner, case):
        target = "conn-missing"
        if case == "wrong-type":
            target = create(local_owner, name="Hook")["id"]
        elif case == "other-install":
            target = create(local_owner, name="Mail", type_="gmail", config={})["id"]
            set_runner(target, "other-install")
        assert configure_google(local_owner).status_code == 200
        response = local_owner.post(GOOGLE + "/start", json={"connector_id": target})
        assert response.status_code == 400
        expected = ("Reconnect this account on the host that runs it" if case == "other-install"
                    else "Gmail connection not found")
        assert response.json() == {"detail": expected}

    def test_too_many_pending_sign_ins_is_429(self, local_owner):
        import time
        assert configure_google(local_owner).status_code == 200
        for i in range(128):
            google._pending[f"s{i}"] = {"expires": time.monotonic() + 600}
        response = local_owner.post(GOOGLE + "/start", json={})
        assert response.status_code == 429
        assert response.json() == {"detail": "Too many pending sign-ins. Try again in a few minutes."}
        # Expired entries are swept first, so the cap frees itself.
        for i in range(128):
            google._pending[f"s{i}"]["expires"] = 0
        assert local_owner.post(GOOGLE + "/start", json={}).status_code == 200

    def test_callback_creates_the_connection_once(self, local_owner, monkeypatch):
        monkeypatch.setattr(google, "_credentials", AsyncMock(return_value=dict(GMAIL_CREDENTIALS)))
        state = google_start(local_owner, name="Work mail").json()["flow_id"]
        response = local_owner.get(google.CALLBACK, params={"state": state, "code": "one-time-code"})
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        # app.main's security headers overwrite the route's Referrer-Policy
        # header, so the page's own meta tag is what keeps the URL private.
        assert '<meta name="referrer" content="no-referrer">' in response.text
        rows = database.get_all_connectors()
        assert len(rows) == 1 and rows[0]["name"] == "Work mail" and rows[0]["type"] == "gmail"
        message = {"type": "connector-oauth", "flow_id": state, "connector_id": rows[0]["id"], "error": None}
        assert json.dumps(message) in response.text
        assert "refresh-secret" not in response.text and "one-time-code" not in response.text
        assert f'gmail_oauth_{state[:16]}=""' in response.headers["set-cookie"]
        # Replaying the same state cannot create a second account.
        replay = local_owner.get(google.CALLBACK, params={"state": state, "code": "one-time-code"})
        assert replay.status_code == 400
        assert len(database.get_all_connectors()) == 1

    def test_callback_rejects_unknown_state_foreign_browser_and_expiry(self, local_owner):
        state = google_start(local_owner).json()["flow_id"]
        unknown = local_owner.get(google.CALLBACK, params={"state": "forged", "code": "c"})
        assert unknown.status_code == 400
        assert unknown.json() == {"detail": "This Google sign-in has expired or belongs to another browser. "
                                            "Start again from Plugins."}
        local_owner.cookies.set("gmail_oauth_" + state[:16], "someone-else", path=google.CALLBACK)
        assert local_owner.get(google.CALLBACK, params={"state": state, "code": "c"}).status_code == 400
        assert state in google._pending  # a foreign browser does not burn the flow
        google._pending[state]["expires"] = 0
        assert local_owner.get(google.CALLBACK, params={"state": state, "code": "c"}).status_code == 400

    def test_callback_on_another_origin_is_400(self, local_owner, make_client):
        response = google_start(local_owner)
        state = response.json()["flow_id"]
        elsewhere = make_client(base_url="http://127.0.0.1")
        elsewhere.cookies.set("gmail_oauth_" + state[:16], response.cookies.get("gmail_oauth_" + state[:16]))
        assert elsewhere.get(google.CALLBACK, params={"state": state, "code": "c"}).status_code == 400
        assert state in google._pending

    def test_token_exchange_failure_reports_a_generic_error(self, local_owner, monkeypatch):
        monkeypatch.setattr(google, "_credentials", AsyncMock(side_effect=RuntimeError("code=abc secret=xyz")))
        state = google_start(local_owner).json()["flow_id"]
        response = local_owner.get(google.CALLBACK, params={"state": state, "code": "abc"})
        assert response.status_code == 200
        assert "Google sign-in could not be saved" in response.text
        assert "xyz" not in response.text
        assert state not in google._pending
        assert database.get_all_connectors() == []

    def test_reconnect_after_the_connection_was_removed(self, local_owner, monkeypatch):
        monkeypatch.setattr(google, "_credentials", AsyncMock(return_value=dict(GMAIL_CREDENTIALS)))
        cid = create(local_owner, name="Mail", type_="gmail", config={})["id"]
        state = google_start(local_owner, connector_id=cid).json()["flow_id"]
        database.delete_connector(cid)
        response = local_owner.get(google.CALLBACK, params={"state": state, "code": "c"})
        assert "The connection was removed while signing in" in response.text
        assert database.get_all_connectors() == []

    def test_callback_page_cannot_be_broken_out_of(self):
        # Regression: the escape used "\u003c" in a normal string literal, which
        # Python reads as "<" itself, so "</script>" passed through unescaped.
        response = google._callback_page("state", "http://localhost", error="</script><script>alert(1)</script>")
        page = response.body.decode()
        script = page.split("<script>", 1)[1]
        assert "</script><script>alert(1)" not in script
        assert script.count("</script>") == 1
        assert "\\u003c/script>" in script


# =============================================================================
# Personal finance (Plaid)
# =============================================================================


PLAID = BASE + "/oauth/plaid"


@pytest.fixture
def https_owner(make_client, bundled_connector_catalog, fake_manager):
    return make_client(base_url="https://localhost")


def configure_plaid(client, **overrides):
    body = {"client_id": "plaid-id", "client_secret": "plaid-secret", "environment": "sandbox",
            "country_codes": ["US"], "redirect_uri": "https://localhost/plaid-link", **overrides}
    return client.put(PLAID + "/app", json=body)


def plaid_backend(accounts=None):
    async def request(config, path, **payload):
        if path == "/link/token/create":
            return {"link_token": "link-sandbox-token"}
        if path == "/item/public_token/exchange":
            return {"access_token": "access-sandbox-secret", "item_id": "item-1"}
        if path == "/accounts/get":
            return {"accounts": accounts if accounts is not None else [{"name": "Checking"}]}
        if path == "/item/remove":
            return {}
        raise AssertionError(path)
    return AsyncMock(side_effect=request)


class TestPlaid:
    def test_status_before_and_after_configuration(self, https_owner):
        assert https_owner.get(PLAID + "/status").json() == {
            "configured": False, "suggested_redirect_uri": "https://localhost/plaid-link"}
        response = configure_plaid(https_owner)
        assert response.status_code == 200 and response.json() == {"configured": True}
        assert https_owner.get(PLAID + "/status").json() == {
            "configured": True, "client_id": "plaid-id", "environment": "sandbox", "country_codes": ["US"],
            "redirect_uri": "https://localhost/plaid-link", "suggested_redirect_uri": "https://localhost/plaid-link"}

    @pytest.mark.parametrize("redirect_uri", [
        "http://localhost/plaid-link", "https://evil.example/plaid-link", "https://localhost/other",
        "https://localhost/plaid-link?x=1", "https://u:p@localhost/plaid-link"])
    def test_redirect_uri_outside_the_allowlist_is_400(self, https_owner, redirect_uri):
        response = configure_plaid(https_owner, redirect_uri=redirect_uri)
        assert response.status_code == 400
        assert response.json()["detail"].startswith("Use this installation's HTTPS origin followed by /plaid-link")
        assert database.get_system_setting(plaid.SETTING) is None

    @pytest.mark.parametrize("override", [
        {"environment": "development"}, {"country_codes": ["ZZ"]}, {"country_codes": []}, {"client_secret": ""}])
    def test_malformed_settings_are_422(self, https_owner, override):
        assert configure_plaid(https_owner, **override).status_code == 422

    def test_locked_credential_storage_is_409(self, https_owner, monkeypatch):
        def locked(_value):
            raise RuntimeError("key unavailable")
        monkeypatch.setattr(encryption, "encrypt_value", locked)
        response = configure_plaid(https_owner)
        assert response.status_code == 409
        assert response.json() == {"detail": "Unlock credential storage before saving Plaid app settings"}

    def test_start_before_configuration_is_400(self, https_owner):
        response = https_owner.post(PLAID + "/start")
        assert response.status_code == 400
        assert response.json() == {"detail": "An administrator must configure Plaid first"}

    def test_start_from_another_origin_is_400(self, https_owner, make_client, monkeypatch):
        backend = plaid_backend()
        monkeypatch.setattr(plaid, "plaid_request", backend)
        configure_plaid(https_owner)
        other = make_client(base_url="https://127.0.0.1")
        assert other.post(PLAID + "/start").status_code == 400
        backend.assert_not_awaited()

    def test_start_sets_a_secure_cookie_scoped_to_the_flow(self, https_owner, monkeypatch):
        backend = plaid_backend()
        monkeypatch.setattr(plaid, "plaid_request", backend)
        configure_plaid(https_owner)
        response = https_owner.post(PLAID + "/start")
        assert response.status_code == 200
        body = response.json()
        assert body["link_token"] == "link-sandbox-token" and set(body) == {"flow_id", "link_token"}
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("plaid_link_" + body["flow_id"][:16] + "=")
        assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/api/v1/connectors/oauth/plaid" in cookie
        assert "Max-Age=1800" in cookie
        kwargs = backend.await_args.kwargs
        assert kwargs["products"] == ["transactions"] and kwargs["redirect_uri"] == "https://localhost/plaid-link"
        assert kwargs["user"] == {"client_user_id": body["flow_id"]}

    def test_plaid_error_on_start_is_400_and_frees_the_slot(self, https_owner, monkeypatch):
        monkeypatch.setattr(plaid, "plaid_request", AsyncMock(side_effect=ValueError("Plaid is unavailable")))
        configure_plaid(https_owner)
        response = https_owner.post(PLAID + "/start")
        assert response.status_code == 400
        assert response.json() == {"detail": "Plaid is unavailable"}
        assert plaid._pending == {}

    @pytest.mark.parametrize("reply", [{}, {"link_token": ""}, {"link_token": None}, {"request_id": "r"}, None])
    def test_plaid_reply_without_link_token_is_502_and_frees_the_slot(self, https_owner, monkeypatch, reply):
        monkeypatch.setattr(plaid, "plaid_request", AsyncMock(return_value=reply))
        configure_plaid(https_owner)
        response = https_owner.post(PLAID + "/start")
        assert response.status_code == 502
        assert response.json() == {
            "detail": "Plaid did not return a link token. Check the Plaid app settings and try again."}
        assert "set-cookie" not in response.headers
        assert plaid._pending == {}

    def test_too_many_pending_connections_is_429(self, https_owner, monkeypatch):
        import time
        monkeypatch.setattr(plaid, "plaid_request", plaid_backend())
        configure_plaid(https_owner)
        for i in range(128):
            plaid._pending[f"f{i}"] = {"expires": time.monotonic() + 1800}
        response = https_owner.post(PLAID + "/start")
        assert response.status_code == 429
        assert response.json() == {"detail": "Too many pending bank connections"}

    def test_exchange_creates_the_connection_once(self, https_owner, monkeypatch):
        monkeypatch.setattr(plaid, "plaid_request", plaid_backend())
        configure_plaid(https_owner)
        flow = https_owner.post(PLAID + "/start").json()["flow_id"]
        response = https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "public-sandbox"})
        assert response.status_code == 200, response.text
        cid = response.json()["connector_id"]
        assert set(response.json()) == {"connector_id"}
        assert "no-store" in response.headers["cache-control"]
        assert f'plaid_link_{flow[:16]}=""' in response.headers["set-cookie"]
        row = database.get_connector(cid)
        assert row["name"] == "Checking (Plaid sandbox)" and row["type"] == "plaid"
        assert "access-sandbox-secret" not in json.dumps(row["config"])
        assert decrypt_config(row["config"], "plaid")["access_token"] == "access-sandbox-secret"
        replay = https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "public-sandbox"})
        assert replay.status_code == 400
        assert len(database.get_all_connectors()) == 1

    def test_exchange_without_accounts_uses_a_generic_name(self, https_owner, monkeypatch):
        monkeypatch.setattr(plaid, "plaid_request", plaid_backend(accounts=[]))
        configure_plaid(https_owner)
        flow = https_owner.post(PLAID + "/start").json()["flow_id"]
        cid = https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "p"}).json()["connector_id"]
        assert database.get_connector(cid)["name"] == "Bank accounts (Plaid sandbox)"

    def test_exchange_rejects_unknown_flow_foreign_browser_and_expiry(self, https_owner, monkeypatch):
        backend = plaid_backend()
        monkeypatch.setattr(plaid, "plaid_request", backend)
        configure_plaid(https_owner)
        flow = https_owner.post(PLAID + "/start").json()["flow_id"]
        backend.reset_mock()
        expected = {"detail": "This bank sign-in expired or belongs to another browser. Start again."}
        unknown = https_owner.post(PLAID + "/exchange", json={"flow_id": "forged", "public_token": "p"})
        assert unknown.status_code == 400 and unknown.json() == expected
        https_owner.cookies.set("plaid_link_" + flow[:16], "someone-else", path="/api/v1/connectors/oauth/plaid")
        foreign = https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "p"})
        assert foreign.status_code == 400 and foreign.json() == expected
        assert flow in plaid._pending
        plaid._pending[flow]["expires"] = 0
        https_owner.cookies.clear()
        assert https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "p"}).status_code == 400
        backend.assert_not_awaited()

    def test_exchange_error_from_plaid_is_400_with_its_message(self, https_owner, monkeypatch):
        monkeypatch.setattr(plaid, "plaid_request", plaid_backend())
        configure_plaid(https_owner)
        flow = https_owner.post(PLAID + "/start").json()["flow_id"]
        monkeypatch.setattr(plaid, "plaid_request", AsyncMock(side_effect=ValueError(
            "Plaid could not complete the request. Check the connection and app configuration.")))
        response = https_owner.post(PLAID + "/exchange", json={"flow_id": flow, "public_token": "p"})
        assert response.status_code == 400
        assert response.json() == {
            "detail": "Plaid could not complete the request. Check the connection and app configuration."}
        assert database.get_all_connectors() == []

    def test_malformed_exchange_body_is_422(self, https_owner):
        assert https_owner.post(PLAID + "/exchange", json={"flow_id": "", "public_token": "p"}).status_code == 422
        assert https_owner.post(PLAID + "/exchange", json={"flow_id": "f"}).status_code == 422


def test_type_catalog_matches_the_types_route(make_client, bundled_connector_catalog, fake_manager):
    """The route is a thin wrapper; it must not drop or reorder types."""
    client = make_client()
    assert [t["type"] for t in client.get(BASE + "/types").json()] == [t["type"] for t in get_connector_type_info()]

