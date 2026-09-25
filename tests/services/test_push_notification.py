"""
Unit tests for push notification service.

Tests cover:
- NotificationPriority enum
- SecurityLevel enum
- NotificationResult dataclass
- PushNotificationService class
- Global instance
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.push_notification import (
    NotificationPriority,
    SecurityLevel,
    NotificationResult,
    PushNotificationService,
    push_notification_service,
    fire_and_forget_notify,
)


class _FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _fake_httpx_client(response):
    """Build a MagicMock that behaves like httpx.AsyncClient(...) as a CM."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


def _offline_resolver(host):
    """Hostnames resolve to a public address without real DNS; IP
    literals go through the real check so private ones are still refused."""
    import ipaddress

    from fluid.tools.builtins.web import resolve_public

    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "93.184.215.14", None
    return resolve_public(host.strip("[]"))


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    monkeypatch.setattr("app.services.push_notification._resolve_public", _offline_resolver)


# =============================================================================
# NotificationPriority Enum Tests
# =============================================================================

class TestNotificationPriority:
    """Test NotificationPriority enum."""

    def test_priority_values(self):
        """NotificationPriority should have expected values."""
        assert NotificationPriority.NORMAL.value == "normal"
        assert NotificationPriority.HIGH.value == "high"

    def test_priority_count(self):
        """Should have expected number of priorities."""
        priorities = list(NotificationPriority)
        assert len(priorities) >= 2


# =============================================================================
# SecurityLevel Enum Tests
# =============================================================================

class TestSecurityLevel:
    """Test SecurityLevel enum."""

    def test_security_level_values(self):
        """SecurityLevel should have expected values."""
        assert SecurityLevel.LOW.value == "low"
        assert SecurityLevel.MEDIUM.value == "medium"
        assert SecurityLevel.HIGH.value == "high"
        assert SecurityLevel.CRITICAL.value == "critical"


# =============================================================================
# NotificationResult Tests
# =============================================================================

class TestNotificationResult:
    """Test NotificationResult dataclass."""

    def test_create_success_result(self):
        """Should create success result."""
        result = NotificationResult(
            success=True,
            message_id="msg-123",
            device_id="device-1",
        )
        assert result.success is True
        assert result.message_id == "msg-123"
        assert result.device_id == "device-1"
        assert result.error is None

    def test_create_error_result(self):
        """Should create error result."""
        result = NotificationResult(
            success=False,
            error="Failed to send",
            device_id="device-1",
        )
        assert result.success is False
        assert result.error == "Failed to send"
        assert result.message_id is None

    def test_result_optional_fields(self):
        """Should have correct optional field defaults."""
        result = NotificationResult(success=True)
        assert result.message_id is None
        assert result.error is None
        assert result.device_id is None


# =============================================================================
# PushNotificationService Tests
# =============================================================================

class TestPushNotificationService:
    """Test PushNotificationService class."""

    def test_create_service(self):
        """Should create service instance."""
        service = PushNotificationService()
        assert service is not None

    def test_service_starts_uninitialized(self):
        """Service should start uninitialized."""
        service = PushNotificationService()
        assert service._initialized is False

    def test_service_mock_mode_default(self):
        """Service should default to mock mode."""
        service = PushNotificationService()
        assert service._mock_mode is True


class TestPushNotificationServiceInitialization:
    """Test PushNotificationService initialization."""

    def test_initialize_without_credentials(self):
        """Should initialize in mock mode without credentials."""
        service = PushNotificationService()
        with patch.dict("os.environ", {}, clear=True):
            result = service.initialize()

        assert result is False  # Returns False for mock mode
        assert service._initialized is True
        assert service._mock_mode is True

    def test_initialize_idempotent(self):
        """Initialize should be idempotent."""
        service = PushNotificationService()
        with patch.dict("os.environ", {}, clear=True):
            result1 = service.initialize()
            result2 = service.initialize()

        assert result1 == result2


# =============================================================================
# Global Instance Tests
# =============================================================================

class TestGlobalInstance:
    """Test global push notification service instance."""

    def test_global_instance_exists(self):
        """Global push notification service instance should exist."""
        assert push_notification_service is not None
        assert isinstance(push_notification_service, PushNotificationService)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_push_notification_module_imports(self):
        """Push notification module should import without errors."""
        from app.services import push_notification as pn_module
        assert pn_module is not None

    def test_enums_importable(self):
        """Enums should be importable."""
        from app.services.push_notification import NotificationPriority, SecurityLevel
        assert NotificationPriority is not None
        assert SecurityLevel is not None

    def test_dataclass_importable(self):
        """NotificationResult should be importable."""
        from app.services.push_notification import NotificationResult
        assert NotificationResult is not None

    def test_class_importable(self):
        """PushNotificationService should be importable."""
        from app.services.push_notification import PushNotificationService
        assert PushNotificationService is not None


# =============================================================================
# UnifiedPush Transport Tests
# =============================================================================

class TestUnifiedPushTransport:
    """Test the UnifiedPush (HTTP POST) delivery path."""

    async def test_send_via_unifiedpush_success(self):
        """A 2xx response from the endpoint should be a successful result."""
        service = PushNotificationService()
        client = _fake_httpx_client(_FakeResponse(200, {"X-Message-ID": "m-1"}))

        with patch("httpx.AsyncClient", return_value=client):
            result = await service.send_notification(
                device_id="device-1",
                title="Done",
                body="Session finished",
                data={"type": "session_done"},
                unifiedpush_endpoint="https://ntfy.sh/up_abc",
            )

        assert result.success is True
        assert result.message_id == "m-1"
        client.post.assert_awaited_once()
        # The endpoint URL is the POST target; payload carries title/body/data.
        args, kwargs = client.post.call_args
        assert args[0] == "https://ntfy.sh/up_abc"
        assert kwargs["json"]["title"] == "Done"
        assert kwargs["json"]["data"] == {"type": "session_done"}

    async def test_send_via_unifiedpush_http_error(self):
        """A non-2xx response should produce a failed result, not raise."""
        service = PushNotificationService()
        client = _fake_httpx_client(_FakeResponse(502))

        with patch("httpx.AsyncClient", return_value=client):
            result = await service.send_notification(
                device_id="device-1",
                title="Done",
                body="Session finished",
                unifiedpush_endpoint="https://ntfy.sh/up_abc",
            )

        assert result.success is False
        assert "502" in (result.error or "")

    async def test_unifiedpush_not_gated_by_mock_mode(self):
        """UnifiedPush should send even when FCM is in mock mode."""
        service = PushNotificationService()
        service._mock_mode = True
        service._initialized = True
        client = _fake_httpx_client(_FakeResponse(200))

        with patch("httpx.AsyncClient", return_value=client):
            result = await service.send_notification(
                device_id="device-1",
                title="T",
                body="B",
                unifiedpush_endpoint="https://ntfy.sh/up_abc",
            )

        # Did not fall through to the mock path; really attempted the POST.
        client.post.assert_awaited_once()
        assert result.success is True

    async def test_send_via_unifiedpush_network_error(self):
        """An httpx exception should be caught and reported as failure."""
        service = PushNotificationService()
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("httpx.AsyncClient", return_value=client):
            result = await service.send_notification(
                device_id="device-1",
                title="T",
                body="B",
                unifiedpush_endpoint="https://ntfy.sh/up_abc",
            )

        assert result.success is False
        assert "boom" in (result.error or "")


# =============================================================================
# Broadcast Dispatch Tests
# =============================================================================

class TestSendToAllDevices:
    """Test per-transport dispatch when broadcasting to all devices."""

    async def test_dispatches_per_transport(self):
        """Each device uses its own transport; tokenless devices are skipped."""
        service = PushNotificationService()
        devices = [
            {"device_id": "d1", "unifiedpush_endpoint": "https://ntfy.sh/a", "fcm_token": None,
             "capabilities": {"supports_push": True}},
            {"device_id": "d2", "unifiedpush_endpoint": None, "fcm_token": "fcm-tok",
             "capabilities": {"supports_push": True}},
            {"device_id": "d3", "unifiedpush_endpoint": None, "fcm_token": None,
             "capabilities": {"supports_push": True}},
        ]
        service.send_notification = AsyncMock(return_value=NotificationResult(success=True))

        with patch("app.db.mobile.get_all_mobile_devices", return_value=devices):
            results = await service.send_to_all_devices(title="T", body="B")

        # d3 has no transport, so only two sends happen.
        assert len(results) == 2
        calls = service.send_notification.await_args_list
        assert calls[0].kwargs["unifiedpush_endpoint"] == "https://ntfy.sh/a"
        assert calls[1].kwargs.get("unifiedpush_endpoint") is None
        assert calls[1].kwargs["device_id"] == "fcm-tok"

    async def test_explicit_tokens_use_fcm_path(self):
        """Passing device_tokens keeps the legacy explicit-FCM behaviour."""
        service = PushNotificationService()
        service.send_notification = AsyncMock(return_value=NotificationResult(success=True))

        results = await service.send_to_all_devices(
            title="T", body="B", device_tokens=["tok-1", "tok-2"]
        )

        assert len(results) == 2
        for call in service.send_notification.await_args_list:
            assert call.kwargs.get("unifiedpush_endpoint") is None

    async def test_registrations_without_push_are_not_posted_to(self):
        """NC-2: current clients declare supports_push false. A legacy
        UnifiedPush endpoint or FCM token on their row is skipped, so the
        title and body never reach a distributor with no app behind it."""
        service = PushNotificationService()
        devices = [
            {"device_id": "old", "unifiedpush_endpoint": "https://ntfy.sh/a", "fcm_token": None,
             "capabilities": {"supports_push": False}},
            {"device_id": "tok", "unifiedpush_endpoint": None, "fcm_token": "fcm-tok",
             "capabilities": {}},
            {"device_id": "raw", "unifiedpush_endpoint": "https://ntfy.sh/b", "fcm_token": None,
             "capabilities": "not-a-dict"},
        ]
        service.send_notification = AsyncMock(return_value=NotificationResult(success=True))

        with patch("app.db.mobile.get_all_mobile_devices", return_value=devices):
            results = await service.send_to_all_devices(title="T", body="B")

        assert results == []
        service.send_notification.assert_not_awaited()

    async def test_no_devices_returns_empty(self):
        """No registered devices yields an empty result list, no error."""
        service = PushNotificationService()

        with patch("app.db.mobile.get_all_mobile_devices", return_value=[]):
            results = await service.send_to_all_devices(title="T", body="B")

        assert results == []


# =============================================================================
# fire_and_forget_notify Tests
# =============================================================================

class TestFireAndForgetNotify:
    """Test the non-blocking broadcast helper used by server hooks."""

    async def test_schedules_broadcast(self):
        """Within a running loop, it schedules a send_to_all_devices call."""
        with patch.object(
            push_notification_service, "send_to_all_devices", new=AsyncMock(return_value=[])
        ) as mock_send:
            fire_and_forget_notify("Title", "Body", {"type": "session_done"})
            # Let the scheduled task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        mock_send.assert_awaited_once()
        assert mock_send.await_args.kwargs["title"] == "Title"
        assert mock_send.await_args.kwargs["data"] == {"type": "session_done"}

    async def test_publishes_event_to_hub(self):
        """It also publishes the event to the SSE hub (embedded-service path)."""
        from app.services.mobile_push_hub import mobile_push_hub

        with patch.object(
            mobile_push_hub, "publish", new=AsyncMock()
        ) as mock_publish, patch.object(
            push_notification_service, "send_to_all_devices", new=AsyncMock(return_value=[])
        ):
            fire_and_forget_notify("Title", "Body", {"type": "session_done"})
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        mock_publish.assert_awaited_once()
        event = mock_publish.await_args.args[0]
        assert event["type"] == "session_done"
        assert event["title"] == "Title"
        assert event["body"] == "Body"
        assert event["data"] == {"type": "session_done"}

    def test_no_running_loop_is_noop(self):
        """Called with no running loop, it must not raise."""
        # Executed in a plain sync test → no running event loop.
        fire_and_forget_notify("Title", "Body")

    async def test_task_is_retained_until_done(self):
        """The scheduled task is strongly held until it finishes, so GC cannot
        collect it mid-run, and it is released afterwards."""
        from app.core import detached_tasks

        gate = asyncio.Event()

        async def _slow(*_a, **_kw):
            await gate.wait()
            return []

        before = set(detached_tasks._DETACHED_TASKS)
        with patch.object(push_notification_service, "send_to_all_devices", new=_slow):
            fire_and_forget_notify("Title", "Body")
            new = detached_tasks._DETACHED_TASKS - before
            assert len(new) == 1
            (task,) = new
            await asyncio.sleep(0)
            assert task in detached_tasks._DETACHED_TASKS
            assert not task.done()
            gate.set()
            await task
        await asyncio.sleep(0)
        assert task not in detached_tasks._DETACHED_TASKS



class TestUnifiedPushEndpointPolicy:
    """Endpoints come from paired devices: HTTPS to a public address only."""

    @pytest.mark.parametrize("endpoint", [
        "http://ntfy.sh/up_abc",
        "https://10.0.0.1/up",
        "https://192.168.1.5/up",
        "https://100.64.0.1/up",
        "https://127.0.0.1:8000/api",
        "https://169.254.169.254/latest",
        "ftp://ntfy.sh/x",
    ])
    def test_refuses_insecure_or_internal_endpoints(self, endpoint):
        from app.services.push_notification import unifiedpush_endpoint_problem

        assert unifiedpush_endpoint_problem(endpoint) is not None

    def test_accepts_public_https_endpoint(self):
        from app.services.push_notification import unifiedpush_endpoint_problem

        assert unifiedpush_endpoint_problem("https://ntfy.sh/up_abc") is None

    async def test_send_refuses_internal_endpoint_without_posting(self):
        service = PushNotificationService()
        client = _fake_httpx_client(_FakeResponse(200))
        with patch("httpx.AsyncClient", return_value=client):
            result = await service.send_notification(
                device_id="device-1", title="T", body="B",
                unifiedpush_endpoint="https://10.0.0.1/up",
            )
        assert result.success is False
        client.post.assert_not_awaited()


# =============================================================================
# Delivery honesty (NC-2)
# =============================================================================

class TestDeliveryHonesty:
    """What counts as "the user was notified"."""

    def test_device_receives_push_needs_the_declared_capability_and_a_transport(self):
        from app.services.push_notification import device_receives_push

        endpoint = {"unifiedpush_endpoint": "https://ntfy.sh/a"}
        assert device_receives_push({**endpoint, "capabilities": {"supports_push": True}})
        assert device_receives_push({"fcm_token": "t", "capabilities": {"supports_push": True}})
        assert not device_receives_push({**endpoint, "capabilities": {"supports_push": False}})
        assert not device_receives_push({**endpoint, "capabilities": {}})
        assert not device_receives_push({**endpoint})
        assert not device_receives_push({**endpoint, "capabilities": {"supports_push": "yes"}})
        assert not device_receives_push({"capabilities": {"supports_push": True}})

    async def test_mock_send_is_marked_simulated(self):
        service = PushNotificationService()
        service._initialized = True
        service._mock_mode = True

        result = await service.send_notification(device_id="tok", title="T", body="B")

        assert result.success is True
        assert result.simulated is True

    def test_delivered_count_skips_failures_and_simulated_sends(self):
        from app.services.push_notification import delivered_count

        assert delivered_count([]) == 0
        assert delivered_count([
            NotificationResult(success=True),
            NotificationResult(success=False),
            NotificationResult(success=True, simulated=True),
        ]) == 1
