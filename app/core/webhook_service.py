"""
Webhook dispatch service for sending events to external integrations.

Handles:
- Dispatching webhook events to subscribed endpoints
- HMAC signature generation for secure webhooks
- Async delivery with retry logic
- Logging of webhook deliveries
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

import httpx

from app.db import database
from app.core.models import WebhookTestResponse

logger = logging.getLogger(__name__)

# Configuration
WEBHOOK_TIMEOUT = 10.0  # seconds
MAX_RETRIES = 3
RETRY_DELAYS = [1, 5, 30]  # seconds between retries


def generate_signature(payload: str, secret: str) -> str:
    """Generate HMAC-SHA256 signature for webhook payload."""
    return hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


async def dispatch_webhook(
    event_type: str,
    data: Dict[str, Any],
    retry: bool = True
) -> None:
    """
    Dispatch a webhook event to all subscribed endpoints.

    This function runs in the background and does not block the main flow.
    Errors are logged but not raised.

    Args:
        event_type: The event type (e.g., "session.complete")
        data: The event data payload
        retry: Whether to retry failed deliveries
    """
    # Get all webhooks subscribed to this event
    webhooks = database.get_webhooks_for_event(event_type)

    if not webhooks:
        logger.debug(f"No webhooks subscribed to event: {event_type}")
        return

    # Build the event payload
    payload = {
        "event": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": data
    }

    # Dispatch to each webhook asynchronously
    tasks = []
    for webhook in webhooks:
        task = asyncio.create_task(
            _deliver_webhook(webhook, payload, retry)
        )
        tasks.append(task)

    # Don't wait for delivery - let it happen in background
    # But log any errors
    if tasks:
        logger.info(f"Dispatching event '{event_type}' to {len(tasks)} webhook(s)")


async def _deliver_webhook(
    webhook: Dict[str, Any],
    payload: Dict[str, Any],
    retry: bool = True
) -> bool:
    """
    Deliver a webhook payload to a single endpoint.

    Args:
        webhook: The webhook configuration
        payload: The event payload to send
        retry: Whether to retry on failure

    Returns:
        True if delivery succeeded, False otherwise
    """
    webhook_id = webhook["id"]
    url = webhook["url"]
    secret = webhook.get("secret")

    payload_json = json.dumps(payload)

    # Build headers
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AI-Hub-Webhook/1.0",
        "X-Webhook-Event": payload["event"],
        "X-Webhook-Delivery-Id": f"{webhook_id}-{int(time.time() * 1000)}",
    }

    # Add signature if secret is configured
    if secret:
        signature = generate_signature(payload_json, secret)
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    # Attempt delivery with retries
    last_error = None
    max_attempts = MAX_RETRIES if retry else 1

    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                response = await client.post(
                    url,
                    content=payload_json,
                    headers=headers
                )

            # Check for success (2xx status codes)
            if 200 <= response.status_code < 300:
                logger.info(
                    f"Webhook delivered successfully: {webhook_id} -> {url} "
                    f"(status={response.status_code})"
                )
                database.update_webhook_triggered(webhook_id, success=True)
                return True
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    f"Webhook delivery failed: {webhook_id} -> {url} "
                    f"(status={response.status_code}, attempt={attempt + 1}/{max_attempts})"
                )

        except httpx.TimeoutException:
            last_error = "Request timed out"
            logger.warning(
                f"Webhook delivery timed out: {webhook_id} -> {url} "
                f"(attempt={attempt + 1}/{max_attempts})"
            )

        except httpx.RequestError as e:
            last_error = str(e)
            logger.warning(
                f"Webhook delivery error: {webhook_id} -> {url} "
                f"(error={e}, attempt={attempt + 1}/{max_attempts})"
            )

        except Exception as e:
            last_error = str(e)
            logger.error(
                f"Unexpected error delivering webhook: {webhook_id} -> {url} "
                f"(error={e}, attempt={attempt + 1}/{max_attempts})"
            )

        # Wait before retry (if not last attempt)
        if attempt < max_attempts - 1 and retry:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            await asyncio.sleep(delay)

    # All attempts failed
    logger.error(
        f"Webhook delivery failed after {max_attempts} attempts: {webhook_id} -> {url} "
        f"(last_error={last_error})"
    )
    database.update_webhook_triggered(webhook_id, success=False)
    return False


async def send_test_webhook(webhook: Dict[str, Any]) -> WebhookTestResponse:
    """
    Send a test webhook to verify the endpoint is working.

    Args:
        webhook: The webhook configuration

    Returns:
        WebhookTestResponse with delivery results
    """
    url = webhook["url"]
    secret = webhook.get("secret")

    # Build test payload
    payload = {
        "event": "test",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": {
            "message": "This is a test webhook from AI Hub",
            "webhook_id": webhook["id"]
        }
    }

    payload_json = json.dumps(payload)

    # Build headers
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AI-Hub-Webhook/1.0",
        "X-Webhook-Event": "test",
        "X-Webhook-Delivery-Id": f"{webhook['id']}-test-{int(time.time() * 1000)}",
    }

    if secret:
        signature = generate_signature(payload_json, secret)
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(
                url,
                content=payload_json,
                headers=headers
            )

        elapsed_ms = int((time.time() - start_time) * 1000)

        if 200 <= response.status_code < 300:
            logger.info(f"Test webhook delivered successfully: {url} (status={response.status_code})")
            return WebhookTestResponse(
                success=True,
                status_code=response.status_code,
                response_time_ms=elapsed_ms
            )
        else:
            logger.warning(f"Test webhook failed: {url} (status={response.status_code})")
            return WebhookTestResponse(
                success=False,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error=f"Server returned {response.status_code}"
            )

    except httpx.TimeoutException:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return WebhookTestResponse(
            success=False,
            response_time_ms=elapsed_ms,
            error="Request timed out"
        )

    except httpx.RequestError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return WebhookTestResponse(
            success=False,
            response_time_ms=elapsed_ms,
            error=str(e)
        )

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return WebhookTestResponse(
            success=False,
            response_time_ms=elapsed_ms,
            error=f"Unexpected error: {e}"
        )


# Convenience functions for common events

async def dispatch_session_complete(
    session_id: str,
    title: Optional[str],
    profile_id: str,
    total_cost: float,
    input_tokens: int,
    output_tokens: int,
    duration_seconds: float
) -> None:
    """Dispatch a session.complete event."""
    await dispatch_webhook(
        event_type="session.complete",
        data={
            "session_id": session_id,
            "title": title,
            "profile_id": profile_id,
            "total_cost": total_cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_seconds": duration_seconds
        }
    )


async def dispatch_session_error(
    session_id: str,
    title: Optional[str],
    profile_id: str,
    error_message: str
) -> None:
    """Dispatch a session.error event."""
    await dispatch_webhook(
        event_type="session.error",
        data={
            "session_id": session_id,
            "title": title,
            "profile_id": profile_id,
            "error_message": error_message
        }
    )


async def dispatch_session_started(
    session_id: str,
    profile_id: str
) -> None:
    """Dispatch a session.started event."""
    await dispatch_webhook(
        event_type="session.started",
        data={
            "session_id": session_id,
            "profile_id": profile_id
        }
    )
