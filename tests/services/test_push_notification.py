"""FCM transport only; all network sends are fake."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.push_notification import (
    DeviceListUnavailable, NotificationPriority, NotificationResult, PushNotificationService,
    SecurityLevel, delivered_count, device_receives_push, fire_and_forget_notify,
)

COMMAND = {"type": "attention", "action": "alert", "worker_id": "worker", "session_id": "chat",
           "version": "1", "episode": "episode", "expires_at": "1234"}


def device(**overrides):
    return {"id": "record", "device_id": "phone", "api_key_hash": "hash", "paired_client_id": "paired",
            "platform": "android", "fcm_token": "token", "capabilities": {"supports_push": True},
            "scopes": ["mobile:notifications"], "revoked_at": None,
            "api_key_expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), **overrides}


def test_capability_and_live_authorization():
    assert device_receives_push(device())
    for overrides in ({"capabilities": {}}, {"capabilities": "invalid"}, {"fcm_token": None},
                      {"platform": "web"}, {"revoked_at": "yesterday"}, {"scopes": []},
                      {"api_key_expires_at": "2020-01-01T00:00:00+00:00"}):
        assert not device_receives_push(device(**overrides))


def test_acceptance_is_not_display_and_compatibility_types():
    assert NotificationPriority.HIGH.value == "high"
    assert SecurityLevel.CRITICAL.value == "critical"
    assert delivered_count([NotificationResult(True), NotificationResult(False), NotificationResult(True, simulated=True)]) == 1


@pytest.mark.asyncio
async def test_no_credentials_is_off_not_a_fake_delivery(monkeypatch):
    from app.services import push_credentials
    monkeypatch.setattr(push_credentials, "load_account", lambda: (None, None))
    service = PushNotificationService()
    result = await service.send_notification("token", "AI Shuffle", "A chat needs you", COMMAND)
    assert not result.success and result.simulated
    assert delivered_count([result]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session_complete", "turn.parked", "watch", "error", "expiry", "update"])
async def test_legacy_senders_cannot_push(kind, monkeypatch):
    service = PushNotificationService()
    configured = Mock(side_effect=AssertionError("must not configure Firebase"))
    monkeypatch.setattr(service, "_configure", configured)
    assert await service.send_to_all_devices("Title", "private text", {"type": kind}) == []
    result = await service.send_notification("token", "Title", "private text", {"type": kind})
    assert not result.success
    fire_and_forget_notify("Title", "private text", {"type": kind})
    configured.assert_not_called()


@pytest.mark.asyncio
async def test_fcm_data_only_high_priority_bounded_collapse(monkeypatch):
    from firebase_admin import messaging
    service = PushNotificationService()
    app = object()
    monkeypatch.setattr(service, "_configure", lambda: app)
    send = Mock(return_value="accepted")
    monkeypatch.setattr(messaging, "send", send)
    result = await service.send_notification("token", "ignored private title", "A chat needs you: Chat",
                                             {**COMMAND, "tool_input": "private tool input"})
    assert result.success and result.message_id == "accepted"
    message = send.call_args.args[0]
    assert message.notification is None
    assert message.data["title"] == "AI Shuffle"
    assert message.data["body"] == "A chat needs you: Chat"
    assert "tool_input" not in message.data
    assert "private" not in str(message.data)
    assert message.android.priority == "high"
    assert message.android.ttl == timedelta(minutes=10)
    assert message.android.collapse_key == "chat"


@pytest.mark.asyncio
async def test_revoked_and_tokenless_rows_skipped_and_invalid_token_pruned(monkeypatch):
    from app.db import mobile
    from app.core import account_hub
    service = PushNotificationService()
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device(), device(fcm_token=None), device(revoked_at="now")])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", lambda d: None)
    monkeypatch.setattr(mobile, "push_device_is_live", lambda d: True)
    prune = Mock()
    monkeypatch.setattr(mobile, "clear_push_token", prune)
    service.send_notification = AsyncMock(return_value=NotificationResult(False, invalid_token=True))
    results = await service.send_to_all_devices("AI Shuffle", "A chat needs you", COMMAND)
    assert len(results) == 1
    service.send_notification.assert_awaited_once()
    prune.assert_called_once_with("record", "token")


@pytest.mark.asyncio
async def test_live_pairing_and_account_checks_win_over_cached_tokens(monkeypatch):
    from app.db import mobile
    from app.core import account_hub
    service = PushNotificationService()
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device()])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", lambda d: None)
    monkeypatch.setattr(mobile, "push_device_is_live", lambda d: False)
    service.send_notification = AsyncMock()
    assert await service.send_to_all_devices("AI Shuffle", "A chat needs you", COMMAND) == []
    service.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_failure_is_not_no_receiver(monkeypatch):
    from app.db import mobile
    def broken():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr(mobile, "get_all_mobile_devices", broken)
    service = PushNotificationService()
    with pytest.raises(DeviceListUnavailable):
        await service.send_to_all_devices("AI Shuffle", "A chat needs you", COMMAND, raise_lookup_errors=True)
