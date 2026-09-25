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
    ) -> List[NotificationResult]:
        """
        Broadcast notification to all registered devices.

        Args:
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            device_tokens: Explicit FCM tokens. If provided, those are used
                directly (legacy/back-compat). If None, every registered device
                is enumerated and dispatched on whichever transport it
                registered — a UnifiedPush endpoint (preferred) or an FCM token.

        Returns:
            List of NotificationResult for each device
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
            return []

        results = []
        for d in devices:
            endpoint = d.get("unifiedpush_endpoint")
            token = d.get("fcm_token")
            if endpoint:
                results.append(
                    await self.send_notification(
                        device_id=d.get("device_id", ""),
                        title=title,
                        body=body,
                        data=data,
                        unifiedpush_endpoint=endpoint,
                    )
                )
            elif token:
                results.append(
                    await self.send_notification(
                        device_id=token, title=title, body=body, data=data
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
    import asyncio

    async def _run() -> None:
        await deliver_local_notification(title, body, data)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("fire_and_forget_notify called with no running loop; skipped")
        return

    loop.create_task(_run())
