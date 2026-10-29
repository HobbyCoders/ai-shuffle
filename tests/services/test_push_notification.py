"""Relay HTTP contract; no network requests or publisher credentials."""
import http.client
import io
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import account_hub, managed_connection
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


@pytest.fixture(autouse=True)
def hub(monkeypatch):
    binding = {"hub_url": account_hub.configured_origin(), "server_id": "00000000-0000-0000-0000-000000000001",
               "active": True, "linked_by": "owner", "encrypted_secret": "test-binding"}
    monkeypatch.setattr(managed_connection, "_linked_binding", lambda: binding)
    monkeypatch.setattr(account_hub, "secret_for", lambda b: "test-worker-secret")
    monkeypatch.setattr(account_hub, "_paused_until", {})
    responses = deque()
    requests = []

    class Socket:
        def makefile(self, *args):
            status, body, headers = responses.popleft()
            raw = json.dumps(body).encode()
            fields = {"Content-Length": str(len(raw)), **headers}
            head = f"HTTP/1.1 {status} Test\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
            return io.BytesIO(head.encode() + b"\r\n" + raw)

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body, headers):
            requests.append((method, path, json.loads(body), headers))

        def getresponse(self):
            response = http.client.HTTPResponse(Socket())
            response.begin()
            return response

        def close(self):
            pass

    monkeypatch.setattr(account_hub, "_PinnedHTTPS", Connection)
    return binding, responses, requests


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
async def test_not_linked_is_off_without_http(monkeypatch, hub):
    def unlinked():
        raise ValueError("Worker unlinked")
    monkeypatch.setattr(managed_connection, "_linked_binding", unlinked)
    service = PushNotificationService()
    result = await service.send_notification("token", "AI Shuffle", "A chat needs you", COMMAND)
    assert not result.success and result.not_linked and result.error == "not_linked"
    assert (await service.send_to_all_devices("AI Shuffle", "A chat needs you", COMMAND))[0].not_linked
    assert not hub[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session_complete", "turn.parked", "watch", "error", "expiry", "update"])
async def test_legacy_senders_cannot_push(kind, hub):
    service = PushNotificationService()
    assert await service.send_to_all_devices("Title", "private text", {"type": kind}) == []
    result = await service.send_notification("token", "Title", "private text", {"type": kind})
    assert not result.success
    fire_and_forget_notify("Title", "private text", {"type": kind})
    assert not hub[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"accepted": True, "message_id": "accepted"}, {"accepted": True, "duplicate": True}])
@pytest.mark.parametrize("action", ["alert", "update", "clear"])
async def test_authenticated_relay_contract_without_chat_text(hub, reply, action):
    binding, responses, requests = hub
    responses.append((200, reply, {}))
    result = await PushNotificationService().send_notification("token", "private title", "private body",
        {**COMMAND, "action": action, "title": "private chat title", "tool_input": "private tool input"})
    assert result.success and result.message_id == reply.get("message_id")
    assert len(requests) == 1
    method, path, body, headers = requests[0]
    assert method == "POST" and path == f"/v1/servers/{binding['server_id']}/push"
    assert headers["Authorization"] == "Bearer test-worker-secret"
    assert body == {"token": "token", "worker_id": "worker", "session_id": "chat", "action": action,
                    "version": "1", "episode": "episode", "expires_at": 1234}
    assert "private" not in json.dumps(body)


@pytest.mark.asyncio
async def test_bad_shape_is_terminal_without_pruning_or_claiming_acceptance(hub):
    hub[1].append((400, {"error": "bad_shape"}, {}))
    result = await PushNotificationService().send_notification("token", "ignored", "ignored", COMMAND)
    assert result.permanent_failure and not result.success and not result.invalid_token
    assert delivered_count([result]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_refusal_is_not_linked_until_relink_not_a_retry_storm(hub, status):
    binding, responses, requests = hub
    responses.append((status, {"error": "unauthorized"}, {}))
    service = PushNotificationService()
    for _ in range(3):
        result = await service.send_notification("token", "ignored", "ignored", COMMAND)
        assert result.not_linked and not result.success
    assert len(requests) == 1 and not service.is_linked()
    binding["encrypted_secret"] = "relinked-test-binding"
    responses.append((200, {"accepted": True}, {}))
    assert (await service.send_notification("token", "ignored", "ignored", COMMAND)).success
    assert service.is_linked() and len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,headers,delay", [
    (429, {"error": "limited"}, {"Retry-After": "90"}, 90),
    (502, {"error": "upstream"}, {}, 15),
    (503, {"error": "push_unconfigured"}, {"Retry-After": "90"}, 90),
    (503, {"error": "push_disabled"}, {}, 15),
])
async def test_retry_later_obeys_retry_after_without_logging(hub, monkeypatch, caplog, status, body, headers, delay):
    _, responses, requests = hub
    clock = [100.0]
    monkeypatch.setattr(account_hub.time, "monotonic", lambda: clock[0])
    responses.append((status, body, headers))
    service = PushNotificationService()
    result = await service.send_notification("token", "ignored", "ignored", COMMAND)
    assert not result.success and not result.not_linked and not result.invalid_token
    assert result.retry_after == delay
    await service.send_notification("token", "ignored", "ignored", COMMAND)
    assert len(requests) == 1
    clock[0] += delay + 1
    responses.append((200, {"accepted": True}, {}))
    assert (await service.send_notification("token", "ignored", "ignored", COMMAND)).success
    assert len(requests) == 2
    assert "push_unconfigured" not in caplog.text and "failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 502, 503])
async def test_real_attention_producer_acknowledges_auth_refusal_but_retries_outages(mock_db, hub, monkeypatch, status):
    from app.db import database as db, interactions, mobile
    from app.db.base import get_db, parse_jsonb
    from app.services import attention_dispatcher as attention
    db.create_session("relay-chat", "default", title="Private title not transmitted")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", ("worker", "relay-chat"))
    interactions.create_interaction("relay-chat", "question", {"question": "Private question"}, 120)
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device()])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", lambda d: None)
    monkeypatch.setattr(mobile, "push_device_is_live", lambda d: True)
    dispatcher = attention.AttentionDispatcher(PushNotificationService())
    monkeypatch.setattr(dispatcher, "worker_id", lambda: "worker")
    monkeypatch.setattr(dispatcher, "private_needs", lambda sid: set())
    hub[1].append((status, {"error": "unavailable"}, {"Retry-After": "90"}))
    result = await dispatcher.reconcile("relay-chat")
    assert not result["sent"]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT push_attention_state FROM sessions WHERE id=%s", ("relay-chat",))
        assert parse_jsonb(c.fetchone()["push_attention_state"])["pending"] is (status not in (401, 403))
    if status in (401, 403):
        assert result["reason"] == "not_linked"
    await dispatcher.reconcile("relay-chat")
    assert len(hub[2]) == 1  # Acked auth refusal, or deferred by Retry-After.
    assert "Private" not in json.dumps(hub[2][0][2])


@pytest.mark.asyncio
async def test_410_prunes_with_compare_and_clear_not_a_rotated_token(hub, monkeypatch):
    from app.db import mobile
    _, responses, _ = hub
    responses.append((410, {"error": "token_invalid"}, {}))
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device()])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", lambda d: None)
    monkeypatch.setattr(mobile, "push_device_is_live", lambda d: True)
    prune = Mock()
    monkeypatch.setattr(mobile, "clear_push_token", prune)
    results = await PushNotificationService().send_to_all_devices("ignored", "ignored", COMMAND)
    assert len(results) == 1 and results[0].invalid_token
    prune.assert_called_once_with("record", "token")


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
async def test_authorization_outage_is_retryable_not_no_receiver(monkeypatch):
    from app.db import mobile
    from fastapi import HTTPException
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device()])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", Mock(side_effect=HTTPException(503, "unavailable")))
    results = await PushNotificationService().send_to_all_devices("ignored", "ignored", COMMAND)
    assert len(results) == 1 and not results[0].success and not results[0].not_linked


@pytest.mark.asyncio
async def test_backoff_defers_before_paused_account_device_verification(hub, monkeypatch):
    from app.db import mobile
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device()])
    hub[1].append((503, {"error": "push_unconfigured"}, {"Retry-After": "90"}))
    service = PushNotificationService()
    assert (await service.send_notification("token", "ignored", "ignored", COMMAND)).retry_after == 90
    verify = Mock(side_effect=AssertionError("must defer before hub ownership lookup"))
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", verify)
    results = await service.send_to_all_devices("ignored", "ignored", COMMAND)
    assert len(results) == 1 and results[0].retry_after > 0
    verify.assert_not_called()
    assert len(hub[2]) == 1


@pytest.mark.asyncio
async def test_lookup_failure_is_not_no_receiver(monkeypatch):
    from app.db import mobile
    def broken():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr(mobile, "get_all_mobile_devices", broken)
    service = PushNotificationService()
    with pytest.raises(DeviceListUnavailable):
        await service.send_to_all_devices("AI Shuffle", "A chat needs you", COMMAND, raise_lookup_errors=True)