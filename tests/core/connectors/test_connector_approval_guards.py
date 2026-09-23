"""Messaging-connector stopgaps (SEC-02, SEC-03).

Approve/Deny presses re-check the sender allowlist, WhatsApp webhooks must be
signed with the connector's app secret, and a connector without an allowlist
does not start.
"""

import hashlib
import hmac
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.connectors.discord_connector import DiscordConnector
from app.core.connector_service import ALLOWLIST_REQUIRED_MESSAGE, missing_sender_allowlist
from app.core.connectors.telegram import TelegramConnector


def _telegram(config):
    return TelegramConnector("tg-1", "telegram", config, AsyncMock(), AsyncMock())


def _callback(chat_id, data="perm:req-1:approve"):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        from_user=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )


async def test_telegram_approval_from_non_allowed_chat_is_ignored():
    conn = _telegram({"allowed_chat_ids": ["111"]})
    await conn._handle_callback_query(_callback(999))
    conn._on_permission_response.assert_not_awaited()


async def test_telegram_action_approval_from_non_allowed_chat_is_ignored():
    conn = _telegram({"allowed_chat_ids": ["111"]})
    fut = MagicMock()
    fut.done.return_value = False
    conn._pending_action_approvals = {"act-1": fut}
    await conn._handle_callback_query(_callback(999, data="actreq:act-1:approve"))
    fut.set_result.assert_not_called()


async def test_telegram_approval_from_allowed_chat_is_applied():
    conn = _telegram({"allowed_chat_ids": ["111"]})
    await conn._handle_callback_query(_callback(111))
    conn._on_permission_response.assert_awaited_once()
    assert conn._on_permission_response.await_args.args[2] is True


def _discord(config):
    return DiscordConnector("dc-1", "discord", config, AsyncMock(), AsyncMock())


def _interaction(channel_id, guild_id="g1"):
    return SimpleNamespace(
        data={"custom_id": "perm:req-1:approve"},
        guild_id=guild_id,
        channel_id=channel_id,
        response=SimpleNamespace(send_message=AsyncMock()),
    )


async def test_discord_approval_from_non_allowed_channel_is_ignored():
    conn = _discord({"allowed_channel_ids": ["c1"]})
    await conn._on_interaction_handler(_interaction("c2"))
    conn._on_permission_response.assert_not_awaited()


async def test_discord_approval_from_non_allowed_guild_is_ignored():
    conn = _discord({"guild_ids": ["g1"]})
    await conn._on_interaction_handler(_interaction("c1", guild_id="g2"))
    conn._on_permission_response.assert_not_awaited()


async def test_discord_approval_from_allowed_channel_is_applied():
    conn = _discord({"allowed_channel_ids": ["c1"]})
    await conn._on_interaction_handler(_interaction("c1"))
    conn._on_permission_response.assert_awaited_once()


# --- WhatsApp webhook signatures ---------------------------------------------

BODY = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn"}}}]}]}).encode()


def _sign(secret: str, body: bytes = BODY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def whatsapp_connector():
    connector = SimpleNamespace(handle_webhook_payload=AsyncMock())
    return connector


def _post(client, connector, config, headers):
    with patch(
        "app.core.connectors.manager.connector_manager.get_running_connectors",
        return_value=[("wa-1", connector)],
    ), patch("app.api.connectors.database.get_connector", return_value={"config": config}), patch(
        "app.core.connector_service.decrypt_config", side_effect=lambda cfg, _t: cfg
    ):
        return client.post(
            "/api/v1/connectors/whatsapp/webhook",
            content=BODY,
            headers={"Content-Type": "application/json", **headers},
        )


def test_whatsapp_unsigned_webhook_is_rejected(client, whatsapp_connector):
    response = _post(client, whatsapp_connector, {"app_secret": "s3cret"}, {})
    assert response.status_code == 403
    whatsapp_connector.handle_webhook_payload.assert_not_awaited()


def test_whatsapp_bad_signature_is_rejected(client, whatsapp_connector):
    response = _post(
        client, whatsapp_connector, {"app_secret": "s3cret"},
        {"X-Hub-Signature-256": _sign("wrong")},
    )
    assert response.status_code == 403
    whatsapp_connector.handle_webhook_payload.assert_not_awaited()


def test_whatsapp_connector_without_app_secret_accepts_nothing(client, whatsapp_connector):
    response = _post(
        client, whatsapp_connector, {}, {"X-Hub-Signature-256": _sign("anything")},
    )
    assert response.status_code == 403
    whatsapp_connector.handle_webhook_payload.assert_not_awaited()


def test_whatsapp_signed_webhook_is_delivered(client, whatsapp_connector):
    response = _post(
        client, whatsapp_connector, {"app_secret": "s3cret"},
        {"X-Hub-Signature-256": _sign("s3cret")},
    )
    assert response.status_code == 200
    whatsapp_connector.handle_webhook_payload.assert_awaited_once()


def test_whatsapp_form_offers_app_secret_and_encrypts_it():
    from app.core.connector_service import get_connector_type_info, get_sensitive_fields

    whatsapp = next(t for t in get_connector_type_info() if t["type"] == "whatsapp")
    field = next(f for f in whatsapp["config_fields"] if f["key"] == "app_secret")
    assert field["type"] == "password" and field["required"] is True
    assert "app_secret" in get_sensitive_fields("whatsapp")


# --- empty allowlist refusal (owner decision D14) --------------------------------


@pytest.mark.parametrize("kind,config", [
    ("telegram", {}),
    ("discord", {"guild_ids": [], "allowed_channel_ids": []}),
    ("whatsapp", {"allowed_phone_numbers": []}),
    ("slack", {"allowed_channel_ids": ""}),
    ("line", {"allowed_group_ids": ["g1"]}),  # groups do not cover direct messages
    ("signal", {}),
    ("imessage", {}),
    ("msteams", {}),
    ("bluebubbles", {}),
])
def test_chat_connector_without_allowlist_is_blocked(kind, config):
    assert missing_sender_allowlist(kind, config) is True


@pytest.mark.parametrize("kind,config", [
    ("telegram", {"allowed_chat_ids": ["1"]}),
    ("discord", {"allowed_channel_ids": ["c1"]}),
    ("discord", {"guild_ids": "g1"}),
    ("whatsapp", {"allowed_phone_numbers": ["15550001"]}),
    ("line", {"allowed_user_ids": ["U1"]}),
    ("webhook", {}),
    ("gmail", {}),
])
def test_allowlisted_or_non_chat_connector_may_start(kind, config):
    assert missing_sender_allowlist(kind, config) is False


async def test_start_refuses_chat_connector_without_allowlist(mock_db, mock_encryption, caplog):
    from app.core.connectors.manager import ConnectorManager
    from app.db import database

    database.create_connector(
        connector_id="tg-open", connector_type="telegram", name="Open bot",
        config={"bot_token": "123:abc"}, events=[], api_user_id=None,
    )
    manager = ConnectorManager()
    with caplog.at_level(logging.ERROR, logger="app.core.connectors.manager"):
        assert await manager.start_connector("tg-open") is False
    assert "tg-open" not in manager._connectors
    row = database.get_connector("tg-open")
    assert row["status"] == "error"
    assert row["status_message"] == ALLOWLIST_REQUIRED_MESSAGE
    assert ALLOWLIST_REQUIRED_MESSAGE in caplog.text


def test_connector_form_marks_allowlist_fields():
    from app.core.connector_service import get_connector_type_info

    telegram = next(t for t in get_connector_type_info() if t["type"] == "telegram")
    field = next(f for f in telegram["config_fields"] if f["key"] == "allowed_chat_ids")
    assert "Required to enable" in field["description"]


def test_activate_without_allowlist_returns_the_form_message(authenticated_client, mock_encryption):
    from app.db import database

    cid = "tg-api-open"
    database.create_connector(
        connector_id=cid, connector_type="telegram", name="Open bot",
        config={"bot_token": "123:abc"}, events=[], api_user_id=None,
    )
    response = authenticated_client.post(f"/api/v1/connectors/{cid}/activate")
    assert response.status_code == 400
    assert response.json()["detail"] == ALLOWLIST_REQUIRED_MESSAGE
