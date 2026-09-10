"""
Tests for connector API endpoints.
"""

import pytest

pytestmark = pytest.mark.usefixtures("bundled_connector_catalog")
from unittest.mock import AsyncMock, patch, MagicMock


# =============================================================================
# List Types & Events
# =============================================================================


class TestListConnectorTypes:
    def test_list_types(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/types")
        assert response.status_code == 200
        types = response.json()
        assert len(types) == 14

    def test_list_types_structure(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/types")
        types = response.json()
        for t in types:
            assert "type" in t
            assert "name" in t
            assert "config_fields" in t

    def test_list_events(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/events")
        assert response.status_code == 200
        events = response.json()
        assert "session.complete" in events
        assert "session.error" in events


# =============================================================================
# CRUD
# =============================================================================


class TestConnectorCRUD:
    def test_list_connectors_empty(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_connector(self, authenticated_client, mock_encryption):
        response = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Test Webhook",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "test"},
                "events": [],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Webhook"
        assert data["type"] == "webhook"

    def test_create_connector_invalid_events(self, authenticated_client, mock_encryption):
        response = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Bad Events",
                "type": "webhook",
                "config": {},
                "events": ["invalid.event"],
            },
        )
        assert response.status_code == 400

    def test_get_connector(self, authenticated_client, mock_encryption):
        # Create first
        create_resp = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Fetch Me",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "fetch"},
                "events": [],
            },
        )
        cid = create_resp.json()["id"]

        # Get it
        response = authenticated_client.get(f"/api/v1/connectors/{cid}")
        assert response.status_code == 200
        assert response.json()["name"] == "Fetch Me"

    def test_get_connector_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/nonexistent")
        assert response.status_code == 404

    def test_delete_connector(self, authenticated_client, mock_encryption):
        # Create first
        create_resp = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Delete Me",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "del"},
                "events": [],
            },
        )
        cid = create_resp.json()["id"]

        response = authenticated_client.delete(f"/api/v1/connectors/{cid}")
        assert response.status_code == 204

        # Confirm gone
        response = authenticated_client.get(f"/api/v1/connectors/{cid}")
        assert response.status_code == 404

    def test_delete_connector_not_found(self, authenticated_client):
        response = authenticated_client.delete("/api/v1/connectors/nonexistent")
        assert response.status_code == 404


# =============================================================================
# Config Masking in Responses
# =============================================================================


class TestConfigMasking:
    def test_list_connectors_masks_secrets(self, authenticated_client, mock_encryption):
        authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Telegram Bot",
                "type": "telegram",
                "config": {"bot_token": "12345678:ABCDEFghij"},
                "events": [],
            },
        )

        response = authenticated_client.get("/api/v1/connectors")
        assert response.status_code == 200
        connectors = response.json()
        assert len(connectors) >= 1

        telegram = next(c for c in connectors if c["type"] == "telegram")
        # bot_token should be masked
        assert "****" in telegram["config"]["bot_token"]


# =============================================================================
# Actions — Test/Status/Logs
# =============================================================================


class TestConnectorActions:
    def test_test_connector_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/connectors/nonexistent/test")
        assert response.status_code == 404

    def test_get_status_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/nonexistent/status")
        assert response.status_code == 404

    def test_get_logs_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/nonexistent/logs")
        assert response.status_code == 404

    def test_get_status_existing(self, authenticated_client, mock_encryption):
        create_resp = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Status Test",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "status"},
                "events": [],
            },
        )
        cid = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/connectors/{cid}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == cid
        assert "status" in data
        assert "is_running" in data

    def test_get_logs_existing(self, authenticated_client, mock_encryption):
        create_resp = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Logs Test",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "logs"},
                "events": [],
            },
        )
        cid = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/connectors/{cid}/logs")
        assert response.status_code == 200
        assert response.json() == []


# =============================================================================
# Discord Guided Setup — /discord/discover
# =============================================================================


class TestDiscordDiscover:
    URL = "/api/v1/connectors/discord/discover"

    def _fake_result(self):
        return {
            "bot": {"id": "111", "name": "TestBot"},
            "application_id": "222",
            "guilds": [
                {
                    "id": "333",
                    "name": "My Server",
                    "channels": [{"id": "444", "name": "general"}],
                    "error": None,
                },
            ],
        }

    def test_discover_with_valid_token(self, authenticated_client):
        with patch(
            "app.core.connectors.discord_connector.discover_resources",
            new=AsyncMock(return_value=self._fake_result()),
        ):
            resp = authenticated_client.post(self.URL, json={"bot_token": "realtoken1234"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["bot"]["name"] == "TestBot"
        assert data["application_id"] == "222"
        assert data["guilds"][0]["channels"][0]["name"] == "general"

    def test_discover_invalid_token(self, authenticated_client):
        with patch(
            "app.core.connectors.discord_connector.discover_resources",
            new=AsyncMock(side_effect=ValueError("Invalid bot token")),
        ):
            resp = authenticated_client.post(self.URL, json={"bot_token": "badtoken"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid bot token"

    def test_discover_upstream_failure_502(self, authenticated_client):
        with patch(
            "app.core.connectors.discord_connector.discover_resources",
            new=AsyncMock(side_effect=TimeoutError("timed out")),
        ):
            resp = authenticated_client.post(self.URL, json={"bot_token": "realtoken1234"})
        assert resp.status_code == 502

    def test_discover_masked_token_rejected(self, authenticated_client):
        resp = authenticated_client.post(self.URL, json={"bot_token": "****abcd"})
        assert resp.status_code == 400

    def test_discover_neither_provided(self, authenticated_client):
        resp = authenticated_client.post(self.URL, json={})
        assert resp.status_code == 400

    def test_discover_both_provided(self, authenticated_client):
        resp = authenticated_client.post(
            self.URL, json={"bot_token": "x", "connector_id": "y"}
        )
        assert resp.status_code == 400

    def test_discover_connector_id_uses_stored_token(self, authenticated_client, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            create = authenticated_client.post(
                "/api/v1/connectors",
                json={
                    "name": "Disco",
                    "type": "discord",
                    "config": {"bot_token": "secrettoken1234"},
                    "events": [],
                },
            )
            assert create.status_code == 201
            cid = create.json()["id"]

            mock = AsyncMock(return_value=self._fake_result())
            with patch(
                "app.core.connectors.discord_connector.discover_resources", new=mock
            ):
                resp = authenticated_client.post(self.URL, json={"connector_id": cid})
        assert resp.status_code == 200
        # The decrypted stored token must be forwarded to discover_resources.
        mock.assert_awaited_once_with("secrettoken1234")

    def test_discover_connector_id_not_found(self, authenticated_client):
        resp = authenticated_client.post(self.URL, json={"connector_id": "nonexistent"})
        assert resp.status_code == 404

    def test_discover_connector_id_wrong_type(self, authenticated_client, mock_encryption):
        create = authenticated_client.post(
            "/api/v1/connectors",
            json={
                "name": "Not Discord",
                "type": "webhook",
                "config": {"mode": "inbound", "inbound_path": "nd"},
                "events": [],
            },
        )
        cid = create.json()["id"]
        resp = authenticated_client.post(self.URL, json={"connector_id": cid})
        assert resp.status_code == 404

    def test_discover_requires_admin(self, client):
        resp = client.post(self.URL, json={"bot_token": "realtoken1234"})
        assert resp.status_code in (401, 403)


# =============================================================================
# WhatsApp Webhook Verification
# =============================================================================


class TestWhatsAppWebhook:
    def test_verify_missing_params(self, authenticated_client):
        response = authenticated_client.get("/api/v1/connectors/whatsapp/webhook")
        assert response.status_code == 400

    def test_verify_invalid_mode(self, authenticated_client):
        response = authenticated_client.get(
            "/api/v1/connectors/whatsapp/webhook",
            params={
                "hub.mode": "invalid",
                "hub.verify_token": "token",
                "hub.challenge": "challenge",
            },
        )
        assert response.status_code == 400

    def test_webhook_post_invalid_json(self, authenticated_client):
        response = authenticated_client.post(
            "/api/v1/connectors/whatsapp/webhook",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
