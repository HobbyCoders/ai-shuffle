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
from datetime import datetime, timezone
from typing import Dict, Any

import httpx

from app.db import database
from app.db.async_database import run_sync
from app.core.models import WebhookTestResponse

logger = logging.getLogger(__name__)

# Configuration
WEBHOOK_TIMEOUT = 10.0  # seconds
MAX_RETRIES = 3
RETRY_DELAYS = [1, 5, 30]  # seconds between retries

# Circuit breaker configuration
CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures before opening circuit
CIRCUIT_BREAKER_COOLDOWN = 300.0  # seconds (5 minutes) before auto-reset

# Circuit breaker state: {webhook_id: {"failures": int, "opened_at": float | None}}
_circuit_breaker_state: Dict[str, Dict[str, Any]] = {}


def _is_circuit_open(webhook_id: str) -> bool:
    """Check if the circuit breaker is open (delivery should be skipped) for a webhook."""
    state = _circuit_breaker_state.get(webhook_id)
    if not state:
        return False
    if state["failures"] < CIRCUIT_BREAKER_THRESHOLD:
        return False
    # Circuit is open - check if cooldown has elapsed for auto-reset
    opened_at = state.get("opened_at")
    if opened_at and (time.time() - opened_at) >= CIRCUIT_BREAKER_COOLDOWN:
        # Cooldown elapsed, reset and allow a probe delivery
        state["failures"] = 0
        state["opened_at"] = None
        logger.info(f"Circuit breaker reset for webhook {webhook_id} after cooldown")
        return False
    return True


def _record_circuit_success(webhook_id: str) -> None:
    """Record a successful delivery, resetting the circuit breaker."""
    if webhook_id in _circuit_breaker_state:
        _circuit_breaker_state[webhook_id] = {"failures": 0, "opened_at": None}


def _record_circuit_failure(webhook_id: str) -> None:
    """Record a failed delivery, potentially opening the circuit breaker."""
    state = _circuit_breaker_state.get(webhook_id)
    if not state:
        state = {"failures": 0, "opened_at": None}
        _circuit_breaker_state[webhook_id] = state
    state["failures"] += 1
    if state["failures"] >= CIRCUIT_BREAKER_THRESHOLD and state["opened_at"] is None:
        state["opened_at"] = time.time()
        logger.warning(
            f"Circuit breaker OPEN for webhook {webhook_id} after "
            f"{state['failures']} consecutive failures. "
            f"Will auto-reset after {CIRCUIT_BREAKER_COOLDOWN}s cooldown."
        )


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
    # Get all webhooks subscribed to this event. One isolated read, so one
    # thread hop — everything after it (task creation) needs the running loop.
    webhooks = await run_sync(database.get_webhooks_for_event, event_type)

    if not webhooks:
        logger.debug(f"No webhooks subscribed to event: {event_type}")
        return

    # Build the event payload
    payload = {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": data
    }

    # Dispatch to each webhook asynchronously
    def _on_delivery_done(t: asyncio.Task) -> None:
        """Log unhandled exceptions from background webhook delivery tasks."""
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(f"Webhook delivery task failed with unhandled exception: {exc}")

    tasks = []
    for webhook in webhooks:
        webhook_id = webhook["id"]
        if _is_circuit_open(webhook_id):
            logger.debug(
                f"Skipping webhook {webhook_id}: circuit breaker is open"
            )
            continue
        task = asyncio.create_task(
            _deliver_webhook(webhook, payload, retry)
        )
        task.add_done_callback(_on_delivery_done)
        tasks.append(task)

    # Don't wait for delivery - let it happen in background
    if tasks:
        logger.debug(f"Dispatching event '{event_type}' to {len(tasks)} webhook(s)")


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
        "User-Agent": "AI-Shuffle-Webhook/1.0",
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
            from app.core.http_client import get_http_client
            client = get_http_client()
            response = await client.post(
                url,
                content=payload_json,
                headers=headers,
                timeout=WEBHOOK_TIMEOUT,
            )

            # Check for success (2xx status codes)
            if 200 <= response.status_code < 300:
                logger.debug(
                    f"Webhook delivered successfully: {webhook_id} -> {url} "
                    f"(status={response.status_code})"
                )
                # Single write sandwiched between network attempts — offload
                # it alone; there is no contiguous DB run to batch it with.
                await run_sync(
                    database.update_webhook_triggered, webhook_id, success=True
                )
                _record_circuit_success(webhook_id)
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
    await run_sync(database.update_webhook_triggered, webhook_id, success=False)
    _record_circuit_failure(webhook_id)
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
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {
            "message": "This is a test webhook from AI Shuffle",
            "webhook_id": webhook["id"]
        }
    }

    payload_json = json.dumps(payload)

    # Build headers
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AI-Shuffle-Webhook/1.0",
        "X-Webhook-Event": "test",
        "X-Webhook-Delivery-Id": f"{webhook['id']}-test-{int(time.time() * 1000)}",
    }

    if secret:
        signature = generate_signature(payload_json, secret)
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    start_time = time.time()

    try:
        from app.core.http_client import get_http_client
        client = get_http_client()
        response = await client.post(
            url,
            content=payload_json,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        if 200 <= response.status_code < 300:
            logger.debug(f"Test webhook delivered successfully: {url} (status={response.status_code})")
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
