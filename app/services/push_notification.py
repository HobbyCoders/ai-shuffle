"""FCM HTTP v1 transport for the attention dispatcher (not the bell).

No sender credential means push is off. FCM acceptance is not proof of display.
Only generic, data-only attention commands reach Android; it owns display and
persistent deduplication, including messages retried after an ambiguous send.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
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


NO_PUSH_RECEIVER_REASON = "no device can receive push notifications"
DEVICE_LIST_UNAVAILABLE_REASON = "the registered device list could not be read"
IN_APP_CHANNEL_NOTE = "The in-app notification center remains available. Sent means accepted by FCM, not displayed or seen."


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
    """Compatibility name: count FCM acceptances, NOT device displays."""
    return sum(r.success and not r.simulated for r in results)


class PushNotificationService:
    def __init__(self):
        self._credential = None
        self._firebase_app = None

    def _configure(self):
        from app.services.push_credentials import load_account
        encrypted, account = load_account()
        if encrypted == self._credential:
            return self._firebase_app
        if self._firebase_app is not None:
            import firebase_admin
            firebase_admin.delete_app(self._firebase_app)
        self._firebase_app = None
        self._credential = None
        if account:
            import firebase_admin
            from firebase_admin import credentials
            self._firebase_app = firebase_admin.initialize_app(
                credentials.Certificate(account), name="attention-push",
                options={"httpTimeout": 10})
            self._credential = encrypted
        return self._firebase_app

    async def send_notification(self, device_id: str, title: str, body: str,
                                data: Optional[Dict[str, Any]] = None,
                                priority: NotificationPriority = NotificationPriority.HIGH) -> NotificationResult:
        # Defence in depth: legacy emitters cannot turn arbitrary text into phone alerts.
        if not data or data.get("type") != "attention" or data.get("action") not in ("alert", "update", "clear"):
            return NotificationResult(False, error="not an attention command")
        try:
            app = await asyncio.to_thread(self._configure)
            if app is None:
                return NotificationResult(False, error="push is off: no sender credentials", simulated=True)
            from firebase_admin import messaging
            allowed = ("type", "action", "worker_id", "session_id", "version", "episode", "expires_at")
            payload = {k: str(data[k]) for k in allowed}
            generic_body = body if body.startswith("A chat needs you: ") and len(body) <= 160 else "A chat needs you"
            payload.update(title="AI Shuffle", body=generic_body)
            message = messaging.Message(data=payload, token=device_id,
                android=messaging.AndroidConfig(priority=priority.value,
                    ttl=timedelta(minutes=10), collapse_key=data["session_id"]))
            response = await asyncio.to_thread(messaging.send, message, app=app)
            return NotificationResult(True, message_id=response)
        except Exception as exc:
            # Never log tokens, credentials, tool input or the SDK error body.
            invalid = type(exc).__name__ in ("UnregisteredError", "SenderIdMismatchError")
            logger.warning("Attention push failed (%s)", type(exc).__name__)
            return NotificationResult(False, error="FCM rejected the send", invalid_token=invalid)

    async def send_to_all_devices(self, title: str, body: str, data=None,
                                  device_tokens=None, raise_lookup_errors=False) -> List[NotificationResult]:
        if not data or data.get("type") != "attention":
            return []
        from app.db import mobile
        from app.db.async_database import run_sync
        from app.core.account_hub import mobile_hub_ownership
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
            except Exception:
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
