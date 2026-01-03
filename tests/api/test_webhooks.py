"""
Comprehensive tests for the webhooks API endpoints.

Tests cover:
- Listing webhooks
- Creating webhooks with validation
- Getting webhook by ID
- Updating webhooks
- Deleting webhooks
- Listing event types
- Testing webhook delivery
- Authentication requirements
- Error handling
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime
from fastapi import HTTPException


# =============================================================================
# Module Import Tests
# =============================================================================

class TestWebhooksModuleImports:
    """Verify webhooks module can be imported correctly."""

    def test_webhooks_module_imports(self):
        """Webhooks module should import without errors."""
        from app.api import webhooks
        assert webhooks is not None

    def test_webhooks_router_exists(self):
        """Webhooks router should exist."""
        from app.api.webhooks import router
        assert router is not None

    def test_valid_event_types_defined(self):
        """VALID_EVENT_TYPES should be defined with expected events."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert isinstance(VALID_EVENT_TYPES, list)
        assert "session.complete" in VALID_EVENT_TYPES
        assert "session.error" in VALID_EVENT_TYPES
        assert "session.started" in VALID_EVENT_TYPES
        assert len(VALID_EVENT_TYPES) == 3


# =============================================================================
# List Webhooks Endpoint Tests
# =============================================================================

class TestListWebhooks:
    """Test GET /api/v1/webhooks endpoint."""

    @pytest.mark.asyncio
    async def test_list_webhooks_success(self):
        """Should return list of webhooks when authorized."""
        from app.api.webhooks import list_webhooks

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_all_webhooks.return_value = [{
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "secret": "secret123",
                "events": ["session.complete"],
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }]

            result = await list_webhooks(token="test-token")

        assert len(result) == 1
        assert result[0]["id"] == "wh-test123"
        assert result[0]["url"] == "https://example.com/webhook"

    @pytest.mark.asyncio
    async def test_list_webhooks_empty(self):
        """Should return empty list when no webhooks exist."""
        from app.api.webhooks import list_webhooks

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_all_webhooks.return_value = []

            result = await list_webhooks(token="test-token")

        assert result == []


# =============================================================================
# List Event Types Endpoint Tests
# =============================================================================

class TestListEventTypes:
    """Test GET /api/v1/webhooks/events endpoint."""

    @pytest.mark.asyncio
    async def test_list_event_types_success(self):
        """Should return list of valid event types."""
        from app.api.webhooks import list_event_types

        result = await list_event_types(token="test-token")

        assert isinstance(result, list)
        assert "session.complete" in result
        assert "session.error" in result
        assert "session.started" in result
        assert len(result) == 3


# =============================================================================
# Create Webhook Endpoint Tests
# =============================================================================

class TestCreateWebhook:
    """Test POST /api/v1/webhooks endpoint."""

    @pytest.mark.asyncio
    async def test_create_webhook_success(self):
        """Should create webhook with valid data."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["session.complete"],
            secret="my-secret-123"
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "my-secret-123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert result["url"] == "https://example.com/webhook"
        assert result["events"] == ["session.complete"]
        assert result["is_active"] is True

    @pytest.mark.asyncio
    async def test_create_webhook_multiple_events(self):
        """Should create webhook with multiple event types."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["session.complete", "session.error", "session.started"],
            secret=None
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": "https://example.com/webhook",
                "events": ["session.complete", "session.error", "session.started"],
                "secret": None,
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert len(result["events"]) == 3

    @pytest.mark.asyncio
    async def test_create_webhook_without_secret(self):
        """Should create webhook without secret."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["session.complete"]
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": None,
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert result["secret"] is None

    @pytest.mark.asyncio
    async def test_create_webhook_invalid_event_type(self):
        """Should reject invalid event types."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["invalid.event"],
            secret=None
        )

        with pytest.raises(HTTPException) as exc_info:
            await create_webhook(webhook_data=webhook_data, token="test-token")

        assert exc_info.value.status_code == 400
        assert "Invalid event type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_webhook_mixed_valid_invalid_events(self):
        """Should reject if any event type is invalid."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["session.complete", "invalid.event"],
            secret=None
        )

        with pytest.raises(HTTPException) as exc_info:
            await create_webhook(webhook_data=webhook_data, token="test-token")

        assert exc_info.value.status_code == 400
        assert "Invalid event type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_webhook_empty_events_list(self):
        """Should create webhook with empty events list."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=[]
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": "https://example.com/webhook",
                "events": [],
                "secret": None,
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert result["events"] == []


# =============================================================================
# Get Webhook Endpoint Tests
# =============================================================================

class TestGetWebhook:
    """Test GET /api/v1/webhooks/{webhook_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_webhook_success(self):
        """Should return webhook by ID."""
        from app.api.webhooks import get_webhook

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await get_webhook(webhook_id="wh-test123", token="test-token")

        assert result["id"] == "wh-test123"
        assert result["url"] == "https://example.com/webhook"

    @pytest.mark.asyncio
    async def test_get_webhook_not_found(self):
        """Should return 404 for non-existent webhook."""
        from app.api.webhooks import get_webhook

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_webhook(webhook_id="wh-nonexistent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


# =============================================================================
# Update Webhook Endpoint Tests
# =============================================================================

class TestUpdateWebhook:
    """Test PATCH /api/v1/webhooks/{webhook_id} endpoint."""

    @pytest.mark.asyncio
    async def test_update_webhook_url(self):
        """Should update webhook URL."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(url="https://new-url.com/webhook")

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://new-url.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["url"] == "https://new-url.com/webhook"

    @pytest.mark.asyncio
    async def test_update_webhook_events(self):
        """Should update webhook events."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(events=["session.complete", "session.error"])

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete", "session.error"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert len(result["events"]) == 2

    @pytest.mark.asyncio
    async def test_update_webhook_secret(self):
        """Should update webhook secret."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(secret="new-secret")

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "new-secret",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["secret"] == "new-secret"

    @pytest.mark.asyncio
    async def test_update_webhook_is_active(self):
        """Should update webhook active status."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(is_active=False)

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": False,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["is_active"] is False

    @pytest.mark.asyncio
    async def test_update_webhook_multiple_fields(self):
        """Should update multiple webhook fields at once."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(
            url="https://new-url.com/webhook",
            events=["session.error"],
            secret="new-secret",
            is_active=False
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://new-url.com/webhook",
                "events": ["session.error"],
                "secret": "new-secret",
                "is_active": False,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["url"] == "https://new-url.com/webhook"
        assert result["events"] == ["session.error"]
        assert result["is_active"] is False

    @pytest.mark.asyncio
    async def test_update_webhook_invalid_event_type(self):
        """Should reject invalid event types on update."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(events=["invalid.event"])

        with pytest.raises(HTTPException) as exc_info:
            await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert exc_info.value.status_code == 400
        assert "Invalid event type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_webhook_not_found(self):
        """Should return 404 for non-existent webhook."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(url="https://new-url.com/webhook")

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await update_webhook(
                    webhook_id="wh-nonexistent",
                    webhook_data=webhook_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_update_webhook_empty_body(self):
        """Should handle update with empty body (no changes)."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate()

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["id"] == "wh-test123"

    @pytest.mark.asyncio
    async def test_update_webhook_null_events_no_validation(self):
        """Should not validate events when events is None (no change)."""
        from app.api.webhooks import update_webhook
        from app.core.models import WebhookUpdate

        webhook_data = WebhookUpdate(url="https://new-url.com/webhook", events=None)

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.update_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://new-url.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await update_webhook(
                webhook_id="wh-test123",
                webhook_data=webhook_data,
                token="test-token"
            )

        assert result["url"] == "https://new-url.com/webhook"


# =============================================================================
# Delete Webhook Endpoint Tests
# =============================================================================

class TestDeleteWebhook:
    """Test DELETE /api/v1/webhooks/{webhook_id} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_webhook_success(self):
        """Should delete webhook."""
        from app.api.webhooks import delete_webhook

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.delete_webhook.return_value = True

            # Should not raise an exception
            result = await delete_webhook(webhook_id="wh-test123", token="test-token")

        mock_db.delete_webhook.assert_called_once_with("wh-test123")
        assert result is None  # 204 No Content

    @pytest.mark.asyncio
    async def test_delete_webhook_not_found(self):
        """Should return 404 for non-existent webhook."""
        from app.api.webhooks import delete_webhook

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.delete_webhook.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                await delete_webhook(webhook_id="wh-nonexistent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


# =============================================================================
# Test Webhook Endpoint Tests
# =============================================================================

class TestTestWebhook:
    """Test POST /api/v1/webhooks/{webhook_id}/test endpoint."""

    @pytest.mark.asyncio
    async def test_test_webhook_success(self):
        """Should send test webhook successfully."""
        from app.api.webhooks import test_webhook
        from app.core.models import WebhookTestResponse

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            with patch("app.core.webhook_service.send_test_webhook") as mock_send:
                mock_send.return_value = WebhookTestResponse(
                    success=True,
                    status_code=200,
                    response_time_ms=150,
                    error=None
                )

                result = await test_webhook(webhook_id="wh-test123", token="test-token")

        assert result.success is True
        assert result.status_code == 200
        assert result.response_time_ms == 150

    @pytest.mark.asyncio
    async def test_test_webhook_failure(self):
        """Should return failure response when test fails."""
        from app.api.webhooks import test_webhook
        from app.core.models import WebhookTestResponse

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            with patch("app.core.webhook_service.send_test_webhook") as mock_send:
                mock_send.return_value = WebhookTestResponse(
                    success=False,
                    status_code=500,
                    response_time_ms=50,
                    error="Server returned 500"
                )

                result = await test_webhook(webhook_id="wh-test123", token="test-token")

        assert result.success is False
        assert result.status_code == 500
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_test_webhook_timeout(self):
        """Should handle timeout during test."""
        from app.api.webhooks import test_webhook
        from app.core.models import WebhookTestResponse

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = {
                "id": "wh-test123",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": "secret123",
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            with patch("app.core.webhook_service.send_test_webhook") as mock_send:
                mock_send.return_value = WebhookTestResponse(
                    success=False,
                    status_code=None,
                    response_time_ms=10000,
                    error="Request timed out"
                )

                result = await test_webhook(webhook_id="wh-test123", token="test-token")

        assert result.success is False
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_test_webhook_not_found(self):
        """Should return 404 for non-existent webhook."""
        from app.api.webhooks import test_webhook

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.get_webhook.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await test_webhook(webhook_id="wh-nonexistent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


# =============================================================================
# Webhook ID Generation Tests
# =============================================================================

class TestWebhookIdGeneration:
    """Test webhook ID generation format."""

    def test_webhook_id_format(self):
        """Webhook IDs should follow expected format."""
        import uuid
        webhook_id = f"wh-{uuid.uuid4().hex[:12]}"
        assert webhook_id.startswith("wh-")
        assert len(webhook_id) == 15  # "wh-" + 12 hex chars

    def test_webhook_id_uniqueness(self):
        """Multiple webhook IDs should be unique."""
        import uuid
        ids = [f"wh-{uuid.uuid4().hex[:12]}" for _ in range(100)]
        assert len(set(ids)) == 100


# =============================================================================
# Event Type Validation Tests
# =============================================================================

class TestEventTypeValidation:
    """Test event type validation logic."""

    def test_valid_session_complete(self):
        """session.complete should be valid."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.complete" in VALID_EVENT_TYPES

    def test_valid_session_error(self):
        """session.error should be valid."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.error" in VALID_EVENT_TYPES

    def test_valid_session_started(self):
        """session.started should be valid."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.started" in VALID_EVENT_TYPES

    @pytest.mark.asyncio
    async def test_all_valid_events_accepted(self):
        """All valid event types should be accepted."""
        from app.api.webhooks import create_webhook, VALID_EVENT_TYPES
        from app.core.models import WebhookCreate

        for event in VALID_EVENT_TYPES:
            webhook_data = WebhookCreate(
                url="https://example.com/webhook",
                events=[event]
            )

            with patch("app.api.webhooks.database") as mock_db:
                mock_db.create_webhook.return_value = {
                    "id": "wh-abc123def456",
                    "url": "https://example.com/webhook",
                    "events": [event],
                    "secret": None,
                    "is_active": True,
                    "created_at": datetime.utcnow().isoformat(),
                    "last_triggered_at": None,
                    "failure_count": 0
                }

                # Should not raise
                result = await create_webhook(webhook_data=webhook_data, token="test-token")
                assert event in result["events"]


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_webhook_with_long_url(self):
        """Should handle very long URLs."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        long_url = "https://example.com/" + "a" * 1000

        webhook_data = WebhookCreate(
            url=long_url,
            events=["session.complete"]
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": long_url,
                "events": ["session.complete"],
                "secret": None,
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert result["url"] == long_url

    @pytest.mark.asyncio
    async def test_webhook_special_characters_in_secret(self):
        """Should handle special characters in secret."""
        from app.api.webhooks import create_webhook
        from app.core.models import WebhookCreate

        special_secret = "s3cr3t!@#$%^&*()_+-=[]{}|;':\",./<>?"

        webhook_data = WebhookCreate(
            url="https://example.com/webhook",
            events=["session.complete"],
            secret=special_secret
        )

        with patch("app.api.webhooks.database") as mock_db:
            mock_db.create_webhook.return_value = {
                "id": "wh-abc123def456",
                "url": "https://example.com/webhook",
                "events": ["session.complete"],
                "secret": special_secret,
                "is_active": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
                "failure_count": 0
            }

            result = await create_webhook(webhook_data=webhook_data, token="test-token")

        assert result["secret"] == special_secret


# =============================================================================
# Database Integration Tests
# =============================================================================

class TestDatabaseIntegration:
    """Test database operations for webhooks."""

    def test_database_webhook_functions_exist(self):
        """Database module should have all webhook functions."""
        from app.db import database
        assert hasattr(database, "get_all_webhooks")
        assert hasattr(database, "get_webhook")
        assert hasattr(database, "create_webhook")
        assert hasattr(database, "update_webhook")
        assert hasattr(database, "delete_webhook")
        assert hasattr(database, "get_active_webhooks")
        assert hasattr(database, "get_webhooks_for_event")
        assert hasattr(database, "update_webhook_triggered")

    def test_webhook_model_fields(self):
        """Webhook model should have expected fields."""
        from app.core.models import Webhook
        # Check model fields exist
        fields = Webhook.model_fields
        assert "id" in fields
        assert "url" in fields
        assert "events" in fields
        assert "secret" in fields
        assert "is_active" in fields
        assert "created_at" in fields
        assert "last_triggered_at" in fields
        assert "failure_count" in fields

    def test_webhook_create_model_fields(self):
        """WebhookCreate model should have expected fields."""
        from app.core.models import WebhookCreate
        fields = WebhookCreate.model_fields
        assert "url" in fields
        assert "events" in fields
        assert "secret" in fields

    def test_webhook_update_model_fields(self):
        """WebhookUpdate model should have expected fields."""
        from app.core.models import WebhookUpdate
        fields = WebhookUpdate.model_fields
        assert "url" in fields
        assert "events" in fields
        assert "secret" in fields
        assert "is_active" in fields

    def test_webhook_test_response_model_fields(self):
        """WebhookTestResponse model should have expected fields."""
        from app.core.models import WebhookTestResponse
        fields = WebhookTestResponse.model_fields
        assert "success" in fields
        assert "status_code" in fields
        assert "response_time_ms" in fields
        assert "error" in fields


# =============================================================================
# Router Configuration Tests
# =============================================================================

class TestRouterConfiguration:
    """Test router configuration."""

    def test_router_prefix(self):
        """Router should have correct prefix."""
        from app.api.webhooks import router
        assert router.prefix == "/api/v1/webhooks"

    def test_router_tags(self):
        """Router should have correct tags."""
        from app.api.webhooks import router
        assert "Webhooks" in router.tags


# =============================================================================
# Valid Event Types Coverage Tests
# =============================================================================

class TestValidEventTypesComplete:
    """Ensure VALID_EVENT_TYPES covers all expected events."""

    def test_session_complete_in_valid_events(self):
        """session.complete should be in VALID_EVENT_TYPES."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.complete" in VALID_EVENT_TYPES

    def test_session_error_in_valid_events(self):
        """session.error should be in VALID_EVENT_TYPES."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.error" in VALID_EVENT_TYPES

    def test_session_started_in_valid_events(self):
        """session.started should be in VALID_EVENT_TYPES."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert "session.started" in VALID_EVENT_TYPES

    def test_valid_event_types_is_list(self):
        """VALID_EVENT_TYPES should be a list."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert isinstance(VALID_EVENT_TYPES, list)

    def test_valid_event_types_count(self):
        """VALID_EVENT_TYPES should have exactly 3 events."""
        from app.api.webhooks import VALID_EVENT_TYPES
        assert len(VALID_EVENT_TYPES) == 3
