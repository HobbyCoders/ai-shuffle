"""
Push notification service for mobile devices using Firebase Cloud Messaging.

Supports both real FCM delivery and mock mode for development without Firebase.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class NotificationPriority(str, Enum):
    """Push notification priority levels."""
    HIGH = "high"
    NORMAL = "normal"


class SecurityLevel(str, Enum):
    """Security level for action requests."""
    LOW = "low"  # Auto-approve after delay
    MEDIUM = "medium"  # Requires user confirmation
    HIGH = "high"  # Requires biometric/PIN verification
    CRITICAL = "critical"  # Requires multi-factor confirmation


@dataclass
class NotificationResult:
    """Result from sending a push notification."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    device_id: Optional[str] = None
    # Mock mode (no Firebase credentials) reports success without anything
    # leaving this server. Callers that tell a person "you were notified" must
    # not count a simulated send.
    simulated: bool = False


#: Why a broadcast reached nobody when no registration can show a notification.
NO_PUSH_RECEIVER_REASON = "no device can receive push notifications"
DEVICE_LIST_UNAVAILABLE_REASON = "the registered device list could not be read"


class DeviceListUnavailable(RuntimeError):
    """The registered devices could not be read, so nothing was attempted."""

#: The channel that does reach the user today.
IN_APP_CHANNEL_NOTE = (
    "The in-app notification center (live notification frames to the open "
    "web, desktop and mobile app) is the working channel; phone push is not."
)


def device_receives_push(device: Dict[str, Any]) -> bool:
    """Whether ``device``'s registration came from a client that raises push.

    A client declares ``capabilities.supports_push`` when it pairs. Every
    current client declares ``false``: the Android app dropped its UnifiedPush
    receiver, and ``ApiClient.registerDevice``, hub enroll and managed repair
    all send ``supports_push: false``. A UnifiedPush endpoint or FCM token on
    such a row points at a distributor with no app to hand the message to, so
    a 2xx from it is not a delivery, and posting the title and body there only
    copies them to a third party.

    A UnifiedPush endpoint is never receivable, whatever the row declares.
    Android builds from abe482c3 (2026-06-24) up to e6c025e1 (2026-09-06)
    registered ``supports_push: true`` together with an endpoint; e6c025e1
    removed the receiver (03cc6ec1 the leftovers). A phone upgraded since then
    never re-registers its capabilities, and the registration stores no
    client version or build date to tell those rows apart, so the endpoint's
    presence is the only reliable marker of that legacy registration.
    """
    capabilities = device.get("capabilities") or {}
    if not isinstance(capabilities, dict) or capabilities.get("supports_push") is not True:
        return False
    if device.get("unifiedpush_endpoint"):
        return False
    return bool(device.get("fcm_token"))


def delivered_count(results: List["NotificationResult"]) -> int:
    """Sends a receiving endpoint acknowledged (2xx), excluding mock sends."""
    return sum(
        1 for r in results
        if getattr(r, "success", False) and not getattr(r, "simulated", False)
    )


def _resolve_public(host: str) -> Tuple[Optional[str], Optional[str]]:
    """``(address, None)`` when every address of ``host`` is public, else
    ``(None, reason)``. Module-level so tests can stub DNS."""
    from fluid.tools.builtins.web import resolve_public

    return resolve_public(host)


def unifiedpush_endpoint_problem(endpoint: str) -> Optional[str]:
    """Why ``endpoint`` may not receive notifications, or None when it may.

    A paired device supplies the endpoint and the worker POSTs to it, so it
    must be HTTPS and resolve only to public addresses: otherwise a device
    could point the worker at services on its own network.
    """
    parsed = urlparse(endpoint or "")
    if parsed.scheme != "https":
        return "UnifiedPush endpoint must be an https:// URL"
    if not parsed.hostname:
        return "UnifiedPush endpoint has no host"
    _address, reason = _resolve_public(parsed.hostname)
    if reason:
        return f"UnifiedPush endpoint is not a public address ({reason})"
    return None


class PushNotificationService:
    """
    Push notification service using Firebase Cloud Messaging.

    Supports mock mode for development when Firebase credentials are not available.
    Set FIREBASE_CREDENTIALS env var to path of service account JSON to enable real FCM.
    """

    def __init__(self):
        self._initialized = False
        self._mock_mode = True
        self._firebase_app = None

    def initialize(self) -> bool:
        """
        Initialize Firebase Admin SDK if credentials are available.

        Returns:
            True if Firebase is initialized, False if running in mock mode.
        """
        if self._initialized:
            return not self._mock_mode

        credentials_path = os.environ.get("FIREBASE_CREDENTIALS")

        if not credentials_path:
            logger.info("FIREBASE_CREDENTIALS not set, running in mock mode")
            self._mock_mode = True
            self._initialized = True
            return False

        if not os.path.exists(credentials_path):
            logger.warning(f"Firebase credentials file not found: {credentials_path}")
            self._mock_mode = True
            self._initialized = True
            return False

        try:
            import firebase_admin
            from firebase_admin import credentials

            cred = credentials.Certificate(credentials_path)
            self._firebase_app = firebase_admin.initialize_app(cred)
            self._mock_mode = False
            self._initialized = True
            logger.info("Firebase Admin SDK initialized successfully")
            return True

        except ImportError:
            logger.warning("firebase-admin not installed, running in mock mode")
            self._mock_mode = True
            self._initialized = True
            return False

        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            self._mock_mode = True
            self._initialized = True
            return False

    async def send_notification(
        self,
        device_id: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        unifiedpush_endpoint: Optional[str] = None,
    ) -> NotificationResult:
        """
        Send a push notification to a specific device.

        Args:
            device_id: FCM registration token (or device id, for UnifiedPush)
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            priority: Notification priority
            unifiedpush_endpoint: If set, deliver via UnifiedPush (HTTP POST to
                this endpoint) instead of FCM. Used for self-hosted servers that
                push to the user's own distributor without Firebase.

        Returns:
            NotificationResult with success status and message ID or error
        """
        if not self._initialized:
            self.initialize()

        notification_data = {
            "device_id": device_id,
            "title": title,
            "body": body,
            "data": data or {},
            "priority": priority.value,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        # UnifiedPush is selected per-device and is NOT gated by FCM mock mode —
        # it is the primary transport for self-hosted servers with no Firebase.
        if unifiedpush_endpoint:
            return await self._send_unifiedpush_notification(
                notification_data, unifiedpush_endpoint
            )

        if self._mock_mode:
            return await self._send_mock_notification(notification_data)

        return await self._send_fcm_notification(notification_data)

    async def _send_mock_notification(
        self, notification_data: Dict[str, Any]
    ) -> NotificationResult:
        """Send notification in mock mode (for development/testing)."""
        mock_message_id = f"mock_{datetime.now(timezone.utc).timestamp()}"
        notification_data["message_id"] = mock_message_id

        logger.debug(
            f"[MOCK] Push notification: {notification_data['title']} -> {notification_data['device_id'][:20]}..."
        )

        return NotificationResult(
            success=True,
            message_id=mock_message_id,
            device_id=notification_data["device_id"],
            simulated=True,
        )

    async def _send_fcm_notification(
        self, notification_data: Dict[str, Any]
    ) -> NotificationResult:
        """Send notification via Firebase Cloud Messaging."""
        try:
            from firebase_admin import messaging

            message = messaging.Message(
                notification=messaging.Notification(
                    title=notification_data["title"],
                    body=notification_data["body"],
                ),
                data={k: str(v) for k, v in notification_data.get("data", {}).items()},
                token=notification_data["device_id"],
                android=messaging.AndroidConfig(
                    priority=notification_data["priority"],
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            alert=messaging.ApsAlert(
                                title=notification_data["title"],
                                body=notification_data["body"],
                            ),
                            sound="default",
                        ),
                    ),
                ),
            )

            response = messaging.send(message)
            notification_data["message_id"] = response

            logger.debug(f"FCM notification sent: {response}")
            return NotificationResult(
                success=True,
                message_id=response,
                device_id=notification_data["device_id"],
            )

        except Exception as e:
            logger.error(f"FCM notification failed: {e}")
            return NotificationResult(
                success=False,
                error=str(e),
                device_id=notification_data["device_id"],
            )

    async def _send_unifiedpush_notification_checked(
        self, notification_data: Dict[str, Any], endpoint: str
    ) -> Optional[NotificationResult]:
        """Refusal result when ``endpoint`` is not a public HTTPS URL, else None."""
        import asyncio

        problem = await asyncio.to_thread(unifiedpush_endpoint_problem, endpoint)
        if problem is None:
            return None
        logger.warning(f"UnifiedPush endpoint refused: {problem}")
        return NotificationResult(
            success=False, error=problem, device_id=notification_data.get("device_id"),
        )

    async def _send_unifiedpush_notification(
        self, notification_data: Dict[str, Any], endpoint: str
    ) -> NotificationResult:
        """Deliver a notification via UnifiedPush (HTTP POST to the device endpoint).

        The body is a small JSON payload the Android app parses to raise a local
        notification (title/body/data + a type for deep-linking into a session).
        This path is independent of FCM/mock mode. The endpoint comes from a
        paired device, so it must be HTTPS to a public address, and the POST
        connects to the address that passed that check.
        """
        refused = await self._send_unifiedpush_notification_checked(notification_data, endpoint)
        if refused is not None:
            return refused
        payload = {
            "title": notification_data["title"],
            "body": notification_data["body"],
            "priority": notification_data.get("priority", NotificationPriority.NORMAL.value),
            "data": notification_data.get("data", {}),
            "timestamp": notification_data.get("timestamp"),
        }
        try:
            import httpx

            from fluid.tools.builtins.web import PinnedTransport

            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                transport=PinnedTransport(resolver=_resolve_public),
            ) as client:
                resp = await client.post(endpoint, json=payload)

            success = 200 <= resp.status_code < 300
            message_id = resp.headers.get("X-Message-ID") if success else None
            notification_data["message_id"] = message_id

            if not success:
                logger.warning(
                    f"UnifiedPush POST returned {resp.status_code} for endpoint "
                    f"{endpoint[:60]}..."
                )
                return NotificationResult(
                    success=False,
                    error=f"HTTP {resp.status_code}",
                    device_id=notification_data.get("device_id"),
                )

            logger.debug(f"UnifiedPush notification delivered to {endpoint[:60]}...")
            return NotificationResult(
                success=True,
                message_id=message_id,
                device_id=notification_data.get("device_id"),
            )

        except Exception as e:
            logger.error(f"UnifiedPush notification failed: {e}")
            return NotificationResult(
                success=False,
                error=str(e),
                device_id=notification_data.get("device_id"),
            )

    async def send_to_all_devices(
        self,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
        device_tokens: Optional[List[str]] = None,
        raise_lookup_errors: bool = False,
    ) -> List[NotificationResult]:
        """
        Broadcast notification to all registered devices.

        Args:
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            device_tokens: Explicit FCM tokens. If provided, those are used
                directly (legacy/back-compat). If None, every registered device
                whose registration can receive push (``device_receives_push``)
                is sent to its FCM token. Other registrations, including
                every legacy UnifiedPush endpoint, are skipped, not posted to.
            raise_lookup_errors: Raise :class:`DeviceListUnavailable` when the
                registered devices cannot be read, instead of returning the
                empty list that also means "no device can receive push".

        Returns:
            List of NotificationResult for each device dispatched to; empty
            when no registration can receive push.
        """
        # Legacy/explicit path: caller passed FCM tokens directly.
        if device_tokens is not None:
            if not device_tokens:
                logger.debug("No devices to broadcast to")
                return []
            results = []
            for token in device_tokens:
                results.append(
                    await self.send_notification(
                        device_id=token, title=title, body=body, data=data
                    )
                )
            successful = sum(1 for r in results if r.success)
            logger.debug(f"Broadcast sent: {successful}/{len(results)} successful")
            return results

        # Default path: enumerate registered devices and dispatch per transport.
        try:
            from app.db import mobile as mobile_db
            from app.db.async_database import run_sync

            devices = await run_sync(mobile_db.get_all_mobile_devices)
        except Exception as e:
            logger.warning(f"Could not enumerate mobile devices for broadcast: {e}")
            if raise_lookup_errors:
                raise DeviceListUnavailable(str(e)) from e
            return []

        results = []
        for d in devices:
            # Only an FCM token can pass: UnifiedPush rows are legacy.
            if not device_receives_push(d):
                continue
            results.append(
                await self.send_notification(
                    device_id=d["fcm_token"], title=title, body=body, data=data
                )
            )

        if not results:
            logger.debug("No devices to broadcast to")
            return results

        successful = sum(1 for r in results if r.success)
        logger.debug(f"Broadcast sent: {successful}/{len(results)} successful")

        return results


# Global service instance
push_notification_service = PushNotificationService()


async def deliver_local_notification(
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Deliver a notification to THIS install's connected devices only.

    Publishes to local SSE streams (the app's embedded foreground service —
    push without Firebase or a UnifiedPush distributor app) and to any locally
    registered UnifiedPush/FCM transports. All errors are swallowed.

    Called by ``fire_and_forget_notify``.
    """
    from app.services.mobile_push_hub import mobile_push_hub

    # Deliver to connected SSE streams (the app's embedded foreground service).
    event = {
        "type": (data or {}).get("type", "notification"),
        "title": title,
        "body": body,
        "data": data or {},
    }
    try:
        await mobile_push_hub.publish(event)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Hub publish failed: {e}")
    # Also deliver to devices that registered a UnifiedPush/FCM transport.
    try:
        await push_notification_service.send_to_all_devices(
            title=title, body=body, data=data
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Background device notification failed: {e}")


def fire_and_forget_notify(
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort broadcast to all registered devices without blocking the caller.

    Used from async hot paths (session completion, user questions, permission
    prompts) where streaming must never block on a slow or unreachable push
    endpoint. Schedules the broadcast as a background task and swallows/logs all
    errors. No-op if called outside a running event loop.

    Device subscriptions live in the shared ``mobile_devices`` table, so a single
    ``deliver_local_notification`` reaches every registered phone cluster-wide —
    no cross-install HTTP fan-out is needed (that legacy relay was removed with
    the V2 placement cutover).
    """
    from app.core.detached_tasks import spawn_detached

    async def _run() -> None:
        await deliver_local_notification(title, body, data)

    if spawn_detached(_run()) is None:
        logger.debug("fire_and_forget_notify called with no running loop; skipped")
