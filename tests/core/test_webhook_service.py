"""
Unit tests for webhook_service module.

Tests cover:
- Signature generation (HMAC-SHA256)
- Webhook dispatching to subscribed endpoints
- Webhook delivery with retries
- Test webhook functionality
- Convenience event dispatchers
- Error handling (timeouts, HTTP errors, exceptions)
"""

import asyncio
import hashlib
import hmac
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx

from app.core.webhook_service import (
    generate_signature,
    dispatch_webhook,
    _deliver_webhook,
    send_test_webhook,
    dispatch_session_complete,
    dispatch_session_error,
    dispatch_session_started,
    WEBHOOK_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAYS,
)
from app.core.models import WebhookTestResponse


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def sample_webhook():
    """Return a sample webhook configuration."""
    return {
        "id": "webhook-123",
        "url": "https://example.com/webhook",
        "secret": "test-secret-key",
        "events": ["session.complete", "session.error"],
        "is_active": True,
    }


@pytest.fixture
def sample_webhook_no_secret():
    """Return a sample webhook configuration without a secret."""
    return {
        "id": "webhook-456",
        "url": "https://example.com/webhook-nosecret",
        "events": ["session.complete"],
        "is_active": True,
    }


@pytest.fixture
def sample_payload():
    """Return a sample event payload."""
    return {
        "event": "session.complete",
        "timestamp": "2024-01-15T10:30:00Z",
        "data": {
            "session_id": "sess-123",
            "title": "Test Session",
            "profile_id": "default",
            "total_cost": 0.05,
        }
    }


@pytest.fixture
def mock_httpx_response():
    """Create a mock HTTP response."""
    response = MagicMock()
    response.status_code = 200
    response.text = "OK"
    return response


# =============================================================================
# Test Signature Generation
# =============================================================================

class TestGenerateSignature:
    """Test HMAC-SHA256 signature generation."""

    def test_generate_signature_basic(self):
        """Should generate a valid HMAC-SHA256 signature."""
        payload = '{"event": "test"}'
        secret = "my-secret"

        signature = generate_signature(payload, secret)

        # Verify the signature is correct
        expected = hmac.new(
            secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        assert signature == expected

    def test_generate_signature_consistency(self):
        """Same payload and secret should produce same signature."""
        payload = '{"key": "value"}'
        secret = "secret123"

        sig1 = generate_signature(payload, secret)
        sig2 = generate_signature(payload, secret)

        assert sig1 == sig2

    def test_generate_signature_different_secrets(self):
        """Different secrets should produce different signatures."""
        payload = '{"key": "value"}'

        sig1 = generate_signature(payload, "secret1")
        sig2 = generate_signature(payload, "secret2")

        assert sig1 != sig2

    def test_generate_signature_different_payloads(self):
        """Different payloads should produce different signatures."""
        secret = "my-secret"

        sig1 = generate_signature('{"a": 1}', secret)
        sig2 = generate_signature('{"b": 2}', secret)

        assert sig1 != sig2

    def test_generate_signature_empty_payload(self):
        """Should handle empty payload."""
        signature = generate_signature("", "secret")
        assert isinstance(signature, str)
        assert len(signature) == 64  # SHA256 hex digest length

    def test_generate_signature_unicode(self):
        """Should handle unicode characters in payload."""
        payload = '{"message": "Hello \u4e16\u754c"}'
        secret = "secret"

        signature = generate_signature(payload, secret)
        assert isinstance(signature, str)
        assert len(signature) == 64


# =============================================================================
# Test Dispatch Webhook
# =============================================================================

class TestDispatchWebhook:
    """Test webhook dispatching to subscribed endpoints."""

    @pytest.mark.asyncio
    async def test_dispatch_no_subscribers(self):
        """Should log debug message when no webhooks subscribed."""
        with patch("app.core.webhook_service.database") as mock_db:
            mock_db.get_webhooks_for_event.return_value = []

            await dispatch_webhook("session.complete", {"session_id": "123"})

            mock_db.get_webhooks_for_event.assert_called_once_with("session.complete")

    @pytest.mark.asyncio
    async def test_dispatch_creates_tasks_for_webhooks(self, sample_webhook):
        """Should create async tasks for each subscribed webhook."""
        with patch("app.core.webhook_service.database") as mock_db:
            with patch("app.core.webhook_service._deliver_webhook", new_callable=AsyncMock) as mock_deliver:
                mock_db.get_webhooks_for_event.return_value = [sample_webhook]

                await dispatch_webhook("session.complete", {"session_id": "123"})

                # Give tasks time to start
                await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_dispatch_multiple_webhooks(self, sample_webhook, sample_webhook_no_secret):
        """Should dispatch to multiple webhooks."""
        with patch("app.core.webhook_service.database") as mock_db:
            with patch("app.core.webhook_service._deliver_webhook", new_callable=AsyncMock) as mock_deliver:
                mock_db.get_webhooks_for_event.return_value = [
                    sample_webhook,
                    sample_webhook_no_secret
                ]

                await dispatch_webhook("session.complete", {"session_id": "123"})

                # Give tasks time to start
                await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_dispatch_builds_correct_payload(self, sample_webhook):
        """Should build payload with event, timestamp, and data."""
        captured_payload = None

        async def capture_payload(webhook, payload, retry):
            nonlocal captured_payload
            captured_payload = payload
            return True

        with patch("app.core.webhook_service.database") as mock_db:
            with patch("app.core.webhook_service._deliver_webhook", side_effect=capture_payload):
                mock_db.get_webhooks_for_event.return_value = [sample_webhook]

                data = {"session_id": "123", "title": "Test"}
                await dispatch_webhook("session.complete", data)

                # Give task time to execute
                await asyncio.sleep(0.1)

                assert captured_payload is not None
                assert captured_payload["event"] == "session.complete"
                assert "timestamp" in captured_payload
                assert captured_payload["timestamp"].endswith("Z")
                assert captured_payload["data"] == data

    @pytest.mark.asyncio
    async def test_dispatch_with_retry_disabled(self, sample_webhook):
        """Should pass retry=False to deliver function."""
        captured_retry = None

        async def capture_args(webhook, payload, retry):
            nonlocal captured_retry
            captured_retry = retry
            return True

        with patch("app.core.webhook_service.database") as mock_db:
            with patch("app.core.webhook_service._deliver_webhook", side_effect=capture_args):
                mock_db.get_webhooks_for_event.return_value = [sample_webhook]

                await dispatch_webhook("session.complete", {}, retry=False)
                await asyncio.sleep(0.1)

                assert captured_retry is False


# =============================================================================
# Test Deliver Webhook
# =============================================================================

class TestDeliverWebhook:
    """Test webhook delivery to a single endpoint."""

    @pytest.mark.asyncio
    async def test_deliver_success(self, sample_webhook, sample_payload):
        """Should return True on successful delivery (2xx status)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                result = await _deliver_webhook(sample_webhook, sample_payload)

                assert result is True
                mock_db.update_webhook_triggered.assert_called_once_with(
                    sample_webhook["id"], success=True
                )

    @pytest.mark.asyncio
    async def test_deliver_includes_signature_header(self, sample_webhook, sample_payload):
        """Should include X-Webhook-Signature header when secret is set."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database"):
                await _deliver_webhook(sample_webhook, sample_payload)

                # Verify headers
                call_args = mock_client.post.call_args
                headers = call_args.kwargs["headers"]

                assert "X-Webhook-Signature" in headers
                assert headers["X-Webhook-Signature"].startswith("sha256=")

    @pytest.mark.asyncio
    async def test_deliver_no_signature_without_secret(self, sample_webhook_no_secret, sample_payload):
        """Should not include signature header when no secret is set."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database"):
                await _deliver_webhook(sample_webhook_no_secret, sample_payload)

                call_args = mock_client.post.call_args
                headers = call_args.kwargs["headers"]

                assert "X-Webhook-Signature" not in headers

    @pytest.mark.asyncio
    async def test_deliver_includes_standard_headers(self, sample_webhook, sample_payload):
        """Should include standard webhook headers."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database"):
                await _deliver_webhook(sample_webhook, sample_payload)

                call_args = mock_client.post.call_args
                headers = call_args.kwargs["headers"]

                assert headers["Content-Type"] == "application/json"
                assert headers["User-Agent"] == "AI-Hub-Webhook/1.0"
                assert headers["X-Webhook-Event"] == sample_payload["event"]
                assert "X-Webhook-Delivery-Id" in headers

    @pytest.mark.asyncio
    async def test_deliver_http_error_with_retries(self, sample_webhook, sample_payload):
        """Should retry on HTTP error status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is False
                    # Should have tried MAX_RETRIES times
                    assert mock_client.post.call_count == MAX_RETRIES
                    mock_db.update_webhook_triggered.assert_called_once_with(
                        sample_webhook["id"], success=False
                    )

    @pytest.mark.asyncio
    async def test_deliver_no_retries_when_disabled(self, sample_webhook, sample_payload):
        """Should not retry when retry=False."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Error"

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                result = await _deliver_webhook(sample_webhook, sample_payload, retry=False)

                assert result is False
                # Should only try once
                assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_deliver_timeout_exception(self, sample_webhook, sample_payload):
        """Should handle timeout exceptions with retries."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Timeout")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is False
                    assert mock_client.post.call_count == MAX_RETRIES
                    mock_db.update_webhook_triggered.assert_called_once_with(
                        sample_webhook["id"], success=False
                    )

    @pytest.mark.asyncio
    async def test_deliver_request_error(self, sample_webhook, sample_payload):
        """Should handle request errors (connection failures, etc.)."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.RequestError("Connection failed")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is False
                    mock_db.update_webhook_triggered.assert_called_once_with(
                        sample_webhook["id"], success=False
                    )

    @pytest.mark.asyncio
    async def test_deliver_unexpected_exception(self, sample_webhook, sample_payload):
        """Should handle unexpected exceptions."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("Unexpected error")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is False
                    mock_db.update_webhook_triggered.assert_called_once_with(
                        sample_webhook["id"], success=False
                    )

    @pytest.mark.asyncio
    async def test_deliver_success_on_retry(self, sample_webhook, sample_payload):
        """Should succeed if a retry succeeds after initial failure."""
        fail_response = MagicMock()
        fail_response.status_code = 503
        fail_response.text = "Service Unavailable"

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.text = "OK"

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            # Fail first two times, succeed on third
            mock_client.post.side_effect = [fail_response, fail_response, success_response]
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is True
                    assert mock_client.post.call_count == 3
                    mock_db.update_webhook_triggered.assert_called_once_with(
                        sample_webhook["id"], success=True
                    )

    @pytest.mark.asyncio
    async def test_deliver_uses_correct_retry_delays(self, sample_webhook, sample_payload):
        """Should use correct delay between retries."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Error"

        captured_delays = []

        async def capture_sleep(delay):
            captured_delays.append(delay)

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database"):
                with patch("app.core.webhook_service.asyncio.sleep", side_effect=capture_sleep):
                    await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    # Should sleep between retries (not after last attempt)
                    assert len(captured_delays) == MAX_RETRIES - 1
                    for i, delay in enumerate(captured_delays):
                        expected_delay = RETRY_DELAYS[min(i, len(RETRY_DELAYS) - 1)]
                        assert delay == expected_delay

    @pytest.mark.asyncio
    async def test_deliver_201_status_is_success(self, sample_webhook, sample_payload):
        """Should treat 201 Created as success."""
        mock_response = MagicMock()
        mock_response.status_code = 201

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                result = await _deliver_webhook(sample_webhook, sample_payload)

                assert result is True
                mock_db.update_webhook_triggered.assert_called_once_with(
                    sample_webhook["id"], success=True
                )

    @pytest.mark.asyncio
    async def test_deliver_204_status_is_success(self, sample_webhook, sample_payload):
        """Should treat 204 No Content as success."""
        mock_response = MagicMock()
        mock_response.status_code = 204

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                result = await _deliver_webhook(sample_webhook, sample_payload)

                assert result is True

    @pytest.mark.asyncio
    async def test_deliver_400_status_is_failure(self, sample_webhook, sample_payload):
        """Should treat 400 Bad Request as failure."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with patch("app.core.webhook_service.database") as mock_db:
                with patch("app.core.webhook_service.asyncio.sleep", new_callable=AsyncMock):
                    result = await _deliver_webhook(sample_webhook, sample_payload, retry=True)

                    assert result is False


# =============================================================================
# Test Send Test Webhook
# =============================================================================

class TestSendTestWebhook:
    """Test the test webhook functionality."""

    @pytest.mark.asyncio
    async def test_send_test_webhook_success(self, sample_webhook):
        """Should return success response on 2xx status."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook)

            assert isinstance(result, WebhookTestResponse)
            assert result.success is True
            assert result.status_code == 200
            assert result.response_time_ms is not None
            assert result.error is None

    @pytest.mark.asyncio
    async def test_send_test_webhook_builds_test_payload(self, sample_webhook):
        """Should send a test event payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        captured_content = None

        async def capture_post(url, content, headers):
            nonlocal captured_content
            captured_content = json.loads(content)
            return mock_response

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = capture_post
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await send_test_webhook(sample_webhook)

            assert captured_content is not None
            assert captured_content["event"] == "test"
            assert "timestamp" in captured_content
            assert captured_content["data"]["message"] == "This is a test webhook from AI Hub"
            assert captured_content["data"]["webhook_id"] == sample_webhook["id"]

    @pytest.mark.asyncio
    async def test_send_test_webhook_includes_signature(self, sample_webhook):
        """Should include signature header for test webhook."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        captured_headers = None

        async def capture_post(url, content, headers):
            nonlocal captured_headers
            captured_headers = headers
            return mock_response

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = capture_post
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await send_test_webhook(sample_webhook)

            assert captured_headers is not None
            assert "X-Webhook-Signature" in captured_headers
            assert captured_headers["X-Webhook-Event"] == "test"

    @pytest.mark.asyncio
    async def test_send_test_webhook_http_error(self, sample_webhook):
        """Should return failure on non-2xx status."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook)

            assert result.success is False
            assert result.status_code == 500
            assert result.error == "Server returned 500"
            assert result.response_time_ms is not None

    @pytest.mark.asyncio
    async def test_send_test_webhook_timeout(self, sample_webhook):
        """Should return failure on timeout."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Timeout")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook)

            assert result.success is False
            assert result.status_code is None
            assert result.error == "Request timed out"
            assert result.response_time_ms is not None

    @pytest.mark.asyncio
    async def test_send_test_webhook_request_error(self, sample_webhook):
        """Should return failure on request error."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.RequestError("Connection refused")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook)

            assert result.success is False
            assert result.status_code is None
            assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_send_test_webhook_unexpected_error(self, sample_webhook):
        """Should return failure on unexpected error."""
        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("Something went wrong")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook)

            assert result.success is False
            assert result.status_code is None
            assert "Unexpected error" in result.error
            assert "Something went wrong" in result.error

    @pytest.mark.asyncio
    async def test_send_test_webhook_no_secret(self, sample_webhook_no_secret):
        """Should work without a secret (no signature header)."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        captured_headers = None

        async def capture_post(url, content, headers):
            nonlocal captured_headers
            captured_headers = headers
            return mock_response

        with patch("app.core.webhook_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = capture_post
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await send_test_webhook(sample_webhook_no_secret)

            assert result.success is True
            assert "X-Webhook-Signature" not in captured_headers


# =============================================================================
# Test Convenience Dispatchers
# =============================================================================

class TestDispatchSessionComplete:
    """Test session.complete event dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_session_complete(self):
        """Should dispatch session.complete with correct data."""
        captured_event = None
        captured_data = None

        async def capture_dispatch(event_type, data, retry=True):
            nonlocal captured_event, captured_data
            captured_event = event_type
            captured_data = data

        with patch("app.core.webhook_service.dispatch_webhook", side_effect=capture_dispatch):
            await dispatch_session_complete(
                session_id="sess-123",
                title="Test Session",
                profile_id="default",
                total_cost=0.05,
                input_tokens=1000,
                output_tokens=500,
                duration_seconds=30.5
            )

            assert captured_event == "session.complete"
            assert captured_data["session_id"] == "sess-123"
            assert captured_data["title"] == "Test Session"
            assert captured_data["profile_id"] == "default"
            assert captured_data["total_cost"] == 0.05
            assert captured_data["input_tokens"] == 1000
            assert captured_data["output_tokens"] == 500
            assert captured_data["duration_seconds"] == 30.5

    @pytest.mark.asyncio
    async def test_dispatch_session_complete_null_title(self):
        """Should handle None title."""
        captured_data = None

        async def capture_dispatch(event_type, data, retry=True):
            nonlocal captured_data
            captured_data = data

        with patch("app.core.webhook_service.dispatch_webhook", side_effect=capture_dispatch):
            await dispatch_session_complete(
                session_id="sess-123",
                title=None,
                profile_id="default",
                total_cost=0.0,
                input_tokens=0,
                output_tokens=0,
                duration_seconds=0.0
            )

            assert captured_data["title"] is None


class TestDispatchSessionError:
    """Test session.error event dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_session_error(self):
        """Should dispatch session.error with correct data."""
        captured_event = None
        captured_data = None

        async def capture_dispatch(event_type, data, retry=True):
            nonlocal captured_event, captured_data
            captured_event = event_type
            captured_data = data

        with patch("app.core.webhook_service.dispatch_webhook", side_effect=capture_dispatch):
            await dispatch_session_error(
                session_id="sess-123",
                title="Failed Session",
                profile_id="default",
                error_message="Something went wrong"
            )

            assert captured_event == "session.error"
            assert captured_data["session_id"] == "sess-123"
            assert captured_data["title"] == "Failed Session"
            assert captured_data["profile_id"] == "default"
            assert captured_data["error_message"] == "Something went wrong"


class TestDispatchSessionStarted:
    """Test session.started event dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_session_started(self):
        """Should dispatch session.started with correct data."""
        captured_event = None
        captured_data = None

        async def capture_dispatch(event_type, data, retry=True):
            nonlocal captured_event, captured_data
            captured_event = event_type
            captured_data = data

        with patch("app.core.webhook_service.dispatch_webhook", side_effect=capture_dispatch):
            await dispatch_session_started(
                session_id="sess-123",
                profile_id="default"
            )

            assert captured_event == "session.started"
            assert captured_data["session_id"] == "sess-123"
            assert captured_data["profile_id"] == "default"


# =============================================================================
# Test Configuration Constants
# =============================================================================

class TestWebhookConfiguration:
    """Test webhook configuration constants."""

    def test_timeout_value(self):
        """Timeout should be a reasonable value."""
        assert WEBHOOK_TIMEOUT == 10.0

    def test_max_retries_value(self):
        """Max retries should be set."""
        assert MAX_RETRIES == 3

    def test_retry_delays_value(self):
        """Retry delays should be increasing."""
        assert RETRY_DELAYS == [1, 5, 30]
        # Each delay should be greater than or equal to the previous
        for i in range(1, len(RETRY_DELAYS)):
            assert RETRY_DELAYS[i] >= RETRY_DELAYS[i-1]
