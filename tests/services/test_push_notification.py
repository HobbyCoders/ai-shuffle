"""Relay HTTP contract; no network requests or publisher credentials."""
import http.client
import io
import json
import logging
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
async def test_transport_failure_warns_without_exception_text_or_token(hub, monkeypatch, caplog):
    monkeypatch.setattr(account_hub, "_PinnedHTTPS", Mock(side_effect=TimeoutError("private-test-sentinel")))
    result = await PushNotificationService().send_notification("private-token", "ignored", "ignored", COMMAND)
    assert not result.success and result.retry_after == 15
    assert result.reason == "relay unavailable (no HTTP response)"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "Attention push failed: relay unavailable (no HTTP response)")]
    assert "private-test-sentinel" not in caplog.text and "private-token" not in caplog.text


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
async def test_retry_later_obeys_retry_after_with_one_warning(hub, monkeypatch, caplog, status, body, headers, delay):
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
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Attention push failed:" in warnings[0].getMessage()


@pytest.fixture
def relay_chat(mock_db, hub, monkeypatch):
    from app.db import database as db, interactions, mobile
    from app.db.base import get_db
    from app.services import attention_dispatcher as attention
    db.create_session("relay-chat", "default", title="Private title not transmitted")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", ("worker", "relay-chat"))
    interactions.create_interaction("relay-chat", "question", {"question": "Private question"}, 120)
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device(fcm_token="private-fcm-token")])
    monkeypatch.setattr(account_hub, "mobile_hub_ownership", lambda d: None)
    monkeypatch.setattr(mobile, "push_device_is_live", lambda d: True)
    dispatcher = attention.AttentionDispatcher(PushNotificationService())
    monkeypatch.setattr(dispatcher, "worker_id", lambda: "worker")
    monkeypatch.setattr(dispatcher, "private_needs", lambda sid: set())
    return dispatcher


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,headers", [
    *[(503, code, {}) for code in (
        "push_disabled", "push_unconfigured", "push_unavailable", "push_upstream_unavailable")],
    *[(502, code, {}) for code in (
        "push_upstream_unavailable", "push_upstream_invalid", "push_upstream_rejected")],
    (429, "push_rate_limited", {"Retry-After": "90"}),
    (401, "unauthorized", {}), (403, "worker_not_registered", {}),
    (410, "token_invalid", {}), (400, "bad_shape", {}),
])
async def test_relay_refusal_reason_and_safe_warning(relay_chat, hub, caplog, status, code, headers):
    from app.db.base import get_db, parse_jsonb
    hub[1].append((status, {"error": code, "detail": "private-fcm-token"}, headers))
    result = await relay_chat.reconcile("relay-chat")
    reason = f"{code} ({status})"
    delay = 90 if status == 429 else 15
    if status not in (400, 401, 403, 410):
        reason += f"; retry after {delay}s"
    assert not result["sent"] and result["reason"] == reason
    if status not in (401, 403):
        assert result["accepted"] == 0
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"Attention push failed: {code} ({status})")]
    for private in ("private-fcm-token", "test-worker-secret", "Private", hub[0]["hub_url"], hub[0]["server_id"]):
        assert private not in caplog.text and private not in json.dumps(result)
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT push_attention_state FROM sessions WHERE id=%s", ("relay-chat",))
        assert parse_jsonb(c.fetchone()["push_attention_state"])["pending"] is (status not in (400, 401, 403, 410))
    retry = await relay_chat.reconcile("relay-chat")
    if status not in (400, 401, 403, 410):
        assert retry["reason"].startswith("relay unavailable; retry after ")
    assert len(hub[2]) == 1  # Terminal ack or existing backoff; neither retries HTTP.
    assert len(caplog.records) == 1
    assert "Private" not in json.dumps(hub[2][0][2])


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"accepted": False}, {}, {"accepted": "true"}])
async def test_nonaccepted_200_explains_failure_and_warns_once(relay_chat, hub, caplog, reply):
    for _ in range(2):
        hub[1].append((200, reply, {}))
        result = await relay_chat.reconcile("relay-chat")
        assert result["sent"] is False and result["accepted"] == 0
        assert result["reason"] == "relay did not accept the send"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "Attention push failed: relay did not accept the send (200)")]
    assert "private-fcm-token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", ["empty", "capability", "ownership", "pairing"])
async def test_no_eligible_phone_reason(relay_chat, hub, monkeypatch, caplog, skip):
    from app.db import mobile
    from fastapi import HTTPException
    if skip == "empty":
        monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [])
    elif skip == "capability":
        monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device(capabilities={})])
    elif skip == "ownership":
        monkeypatch.setattr(account_hub, "mobile_hub_ownership", Mock(side_effect=HTTPException(403, "private")))
    else:
        monkeypatch.setattr(mobile, "push_device_is_live", lambda d: False)
    result = await relay_chat.reconcile("relay-chat")
    assert result["sent"] is False and result["accepted"] == 0
    assert result["reason"] == "no device can receive push notifications"
    assert not hub[2] and not caplog.records  # No relay request, so no relay warning.


@pytest.mark.asyncio
async def test_success_result_unchanged(relay_chat, hub, caplog):
    hub[1].append((200, {"accepted": True}, {}))
    assert await relay_chat.reconcile("relay-chat") == {
        "sent": True, "accepted": 1,
        "note": "Relay acceptance is not proof of display or that the owner has seen it."}
    assert not caplog.records
    assert await relay_chat.reconcile("relay-chat") == {
        "sent": False, "deduplicated": True, "reason": "attention already reconciled"}


@pytest.mark.asyncio
async def test_expired_command_is_not_misreported_as_no_phone(relay_chat, hub, caplog):
    from app.db.base import get_db, parse_jsonb
    hub[1].append((200, {"accepted": False}, {}))
    await relay_chat.reconcile("relay-chat")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT push_attention_state FROM sessions WHERE id=%s", ("relay-chat",))
        state = parse_jsonb(c.fetchone()["push_attention_state"])
        state["enqueued_at"] = 0
        c.execute("UPDATE sessions SET push_attention_state=%s WHERE id=%s", (json.dumps(state), "relay-chat"))
    caplog.clear()
    result = await relay_chat.reconcile("relay-chat")
    assert not result["sent"] and result["accepted"] == 0 and result["reason"] == "attention expired"
    assert len(hub[2]) == 1 and not caplog.records


@pytest.mark.asyncio
async def test_partial_acceptance_does_not_add_a_failure_reason(relay_chat, hub, monkeypatch, caplog):
    from app.db import mobile
    monkeypatch.setattr(mobile, "get_all_mobile_devices", lambda: [device(), device(id="second")])
    hub[1].extend([(200, {"accepted": False}, {}), (200, {"accepted": True}, {})])
    result = await relay_chat.reconcile("relay-chat")
    assert result["sent"] is True and result["accepted"] == 1 and "reason" not in result
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_transport_failure_reason_reaches_dispatcher(relay_chat, monkeypatch, caplog):
    monkeypatch.setattr(account_hub, "_PinnedHTTPS", Mock(side_effect=TimeoutError("private-fcm-token")))
    result = await relay_chat.reconcile("relay-chat")
    assert result["sent"] is False and result["accepted"] == 0
    assert result["reason"] == "relay unavailable (no HTTP response); retry after 15s"
    assert [r.getMessage() for r in caplog.records] == [
        "Attention push failed: relay unavailable (no HTTP response)"]
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert "private-fcm-token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"error": "private-fcm-token"}, {"error": "push_disabled\nprivate-fcm-token"},
    {"error": ["private-fcm-token"]}, ["private-fcm-token"], "private-fcm-token",
    {"error": "push_disabled", "detail": "private-fcm-token" * 500},
])
async def test_untrusted_error_body_is_never_exposed(relay_chat, hub, caplog, body):
    hub[1].append((503, body, {}))
    result = await relay_chat.reconcile("relay-chat")
    assert result["reason"] == "relay unavailable (503); retry after 15s"
    assert [r.getMessage() for r in caplog.records] == ["Attention push failed: relay unavailable (503)"]
    assert "private-fcm-token" not in caplog.text and "private-fcm-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_warnings_deduplicate_per_code_and_expire_without_changing_backoff(hub, monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr(account_hub.time, "monotonic", lambda: clock[0])
    service = PushNotificationService()
    for now, code in ((100, "push_unavailable"), (116, "push_unavailable"),
                      (132, "push_disabled"), (401, "push_unavailable")):
        clock[0] = now
        hub[1].append((503, {"error": code}, {}))
        result = await service.send_notification("private-fcm-token", "ignored", "ignored", COMMAND)
        assert result.retry_after == 15 and result.reason == f"{code} (503)"
    assert len(hub[2]) == 4
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"Attention push failed: {code} (503)")
        for code in ("push_unavailable", "push_disabled", "push_unavailable")]
    assert "private-fcm-token" not in caplog.text


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