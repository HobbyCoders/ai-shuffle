"""Account-hub relay transport for the attention dispatcher (not the bell).

Only signed-in, linked workers can send. No publisher credential or chat text
leaves the worker. Android owns display and persistent version/episode receipts;
relay acceptance is not proof that a notification was displayed or seen.
"""
import asyncio
import logging
import math
from dataclasses import dataclass
import time
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NotificationPriority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"


class SecurityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class NotificationResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    device_id: Optional[str] = None
    simulated: bool = False
    invalid_token: bool = False
    not_linked: bool = False
    retry_after: Optional[float] = None
    permanent_failure: bool = False
    reason: Optional[str] = None


NO_PUSH_RECEIVER_REASON = "no device can receive push notifications"
DEVICE_LIST_UNAVAILABLE_REASON = "the registered device list could not be read"
IN_APP_CHANNEL_NOTE = "The in-app notification center remains available. Sent means accepted by the relay, not displayed or seen."


class DeviceListUnavailable(RuntimeError):
    pass


def device_receives_push(device: Dict[str, Any]) -> bool:
    from app.core.device_keys import key_expiry
    from datetime import datetime, timezone
    caps = device.get("capabilities") or {}
    return (isinstance(caps, dict) and caps.get("supports_push") is True
            and bool(device.get("fcm_token")) and not device.get("revoked_at")
            and device.get("platform") == "android"
            and "mobile:notifications" in (device.get("scopes") or [])
            and key_expiry(device) > datetime.now(timezone.utc))


def delivered_count(results: List[NotificationResult]) -> int:
    """Compatibility name: count relay acceptances, NOT device displays."""
    return sum(r.success and not r.simulated for r in results)


def failure_reason(results: List[NotificationResult]) -> str:
    if not results:
        return NO_PUSH_RECEIVER_REASON
    failure = next((r for r in results if r.reason), results[0])
    reason = failure.reason or failure.error or ("not_linked" if failure.not_linked else "relay did not accept the send")
    if failure.retry_after is not None:
        reason += f"; retry after {math.ceil(failure.retry_after)}s"
    return reason


class PushNotificationService:
    def __init__(self):
        self._rejected_binding = None
        self._retry_binding = None
        self._retry_at = 0.0
        self._warning_at = {}

    def _warn_failure(self, reason):
        # Only fixed messages / allow-listed hub codes and HTTP status reach here.
        # One warning per outcome per five minutes, independent of send backoff.
        now = time.monotonic()
        if now >= self._warning_at.get(reason, 0.0):
            self._warning_at[reason] = now + 300.0
            logger.warning("Attention push failed: %s", reason)

    @staticmethod
    def _identity(binding):
        return tuple(binding.get(k) for k in (
            "hub_url", "server_id", "linked_by", "encrypted_secret", "instance_id"))

    def linked_binding(self):
        from app.core.managed_connection import _linked_binding
        try:
            binding = _linked_binding()
        except ValueError:
            return None
        # An auth refusal is terminal until the owner relinks. Never turn every
        # outbox scan (or new chat) into another rejected HTTP request.
        return binding if self._identity(binding) != self._rejected_binding else None

    def is_linked(self):
        return self.linked_binding() is not None

    async def send_notification(self, device_id: str, title: str, body: str,
                                data: Optional[Dict[str, Any]] = None,
                                priority: NotificationPriority = NotificationPriority.HIGH) -> NotificationResult:
        # Defence in depth: legacy emitters cannot send arbitrary text to phones.
        if not data or data.get("type") != "attention" or data.get("action") not in ("alert", "update", "clear"):
            return NotificationResult(False, error="not an attention command")
        binding = await asyncio.to_thread(self.linked_binding)
        if binding is None:
            return NotificationResult(False, error="not_linked", not_linked=True)
        identity = self._identity(binding)
        if identity == self._retry_binding and time.monotonic() < self._retry_at:
            return NotificationResult(False, error="relay unavailable", retry_after=self._retry_at - time.monotonic())
        from app.core import account_hub
        try:
            # The relay supplies the fixed title/body. Never forward caller text,
            # the chat title, or arbitrary data (even from old persisted states).
            payload = {k: data[k] for k in ("worker_id", "session_id", "action", "version", "episode")}
            payload.update(token=device_id, expires_at=int(data["expires_at"]))
            result = await asyncio.to_thread(account_hub.call, binding, "POST", "push", payload)
            if result.get("accepted") is not True:
                self._warn_failure("relay did not accept the send (200)")
                return NotificationResult(False, error="relay did not accept the send")
            return NotificationResult(True, message_id=result.get("message_id"))
        except Exception as exc:
            status = account_hub.hub_status(exc)
            code = getattr(exc, "hub_error", None) or "relay unavailable"
            reason = f"{code} ({status if status is not None else 'no HTTP response'})"
            if status == 410:
                # Routine pruning of an uninstalled app's token, not a relay fault.
                return NotificationResult(False, error="token_invalid", invalid_token=True, reason=reason)
            if status in (401, 403):
                # Terminal until relink; keep the documented not_linked reason.
                reason = f"not_linked: {getattr(exc, 'hub_error', None) or 'refused'} ({status})"
                self._warn_failure(reason)
                self._rejected_binding = identity
                return NotificationResult(False, error="not_linked", not_linked=True, reason=reason)
            self._warn_failure(reason)
            if status == 400:
                # A malformed command cannot be repaired by retrying it.
                return NotificationResult(False, error="invalid push command", permanent_failure=True, reason=reason)
            delay = account_hub.retry_after(exc)
            delay = max(15.0, delay or 0.0)
            self._retry_binding, self._retry_at = identity, time.monotonic() + delay
            return NotificationResult(False, error="relay unavailable", retry_after=delay, reason=reason)

    async def send_to_all_devices(self, title: str, body: str, data=None,
                                  device_tokens=None, raise_lookup_errors=False) -> List[NotificationResult]:
        if not data or data.get("type") != "attention":
            return []
        from app.db import mobile
        from app.db.async_database import run_sync
        from app.core.account_hub import mobile_hub_ownership
        binding = await run_sync(self.linked_binding)
        if binding is None:
            return [NotificationResult(False, error="not_linked", not_linked=True)]
        # Check backoff before account-device verification: the shared hub client
        # may be paused too, and that is not evidence that there are no recipients.
        if self._identity(binding) == self._retry_binding and time.monotonic() < self._retry_at:
            return [NotificationResult(False, error="relay unavailable", retry_after=self._retry_at - time.monotonic())]
        try:
            devices = await run_sync(mobile.get_all_mobile_devices)
        except Exception:
            if raise_lookup_errors:
                raise DeviceListUnavailable(DEVICE_LIST_UNAVAILABLE_REASON) from None
            return []
        results = []
        for device in devices:
            if not device_receives_push(device):
                continue
            try:
                # Includes account ownership and worker-confirmation revocation.
                await run_sync(mobile_hub_ownership, device)
                if not await run_sync(mobile.push_device_is_live, device):
                    continue
            except Exception as exc:
                from fastapi import HTTPException
                if not isinstance(exc, HTTPException) or exc.status_code >= 500 or exc.status_code == 429:
                    # An unavailable authorization/pairing lookup must not ack
                    # the durable outbox as though every device was revoked.
                    results.append(NotificationResult(False, error="device authorization unavailable"))
                continue
            result = await self.send_notification(device["fcm_token"], title, body, data)
            if result.invalid_token:
                await run_sync(mobile.clear_push_token, device["id"], device["fcm_token"])
            results.append(result)
        return results


push_notification_service = PushNotificationService()


def fire_and_forget_notify(title: str, body: str, data=None) -> None:
    """Retired automatic adapter. Bell, watches and lifecycle events never push."""
    return None


async def deliver_local_notification(title: str, body: str, data=None) -> None:
    """Retired automatic adapter; only attention_dispatcher may send."""
    return None
