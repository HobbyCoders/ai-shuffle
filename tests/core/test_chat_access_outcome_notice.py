"""The owner's answer to a service access card reaches the requesting chat (#1420).

A service request returns ``awaiting_user`` at once and the card stays open, so
nothing waits on the tool call. Approving or declining it must tell the chat
without an owner message: a steer into a running turn, otherwise a queued wake.
"""
import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from app.core import chat_access, turn_scheduler
from app.core.chat_access import Broker, Grant, GrantInput, GrantSubmission
from app.db import database, turns
from tests.core.test_session_orchestration import chat

_OWNER = ("local", "worker")
_SECRET = "token-value-never-shown-123"
_URL = "https://api.private-example.test/v1"
_LABEL = "Owner Private Label"


@pytest.fixture
def asker(mock_db, monkeypatch):
    from app.core import composer_suggestions
    from app.core.task_registry import task_registry

    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    monkeypatch.setattr(composer_suggestions, "schedule_suggestion", lambda *args: None)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=_OWNER))
    monkeypatch.setattr(chat_access, "publish_change", AsyncMock())
    monkeypatch.setattr(Broker, "_defer_registered_away", AsyncMock())
    task_registry._woken.clear()
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    chat("asker")
    broker = Broker()

    async def create(session_id, spec, *, request=None, plugin=None):
        # The real create pins the service's certificate over the network.
        grant = Grant(uuid.uuid4().hex, session_id, _OWNER, spec.label, spec.kind, spec.url,
                      time.time() + spec.minutes * 60, writable=spec.writable, ready=False,
                      secret=spec.secret.get_secret_value())
        broker.grants[grant.id] = grant
        return grant.public()

    monkeypatch.setattr(broker, "create", create)
    yield broker
    task_registry._woken.clear()


async def _settle(broker):
    async with asyncio.timeout(5):
        while broker._notifications:
            await asyncio.gather(*list(broker._notifications), return_exceptions=True)


def _submission():
    return GrantSubmission(connection=GrantInput(kind="service", label=_LABEL, url=_URL, secret=_SECRET),
                           minutes=30)


def _wakes(session_id):
    return [t for t in turns.get_queued_turns(session_id) if t.get("source") == "unpark"]


@pytest.mark.asyncio
async def test_idle_chat_is_woken_once_with_the_usable_handle(asker):
    pending = await asker.request("asker", "service", "Read DNS records", service_provider="custom")
    assert pending["status"] == "awaiting_user"
    await _settle(asker)
    assert _wakes("asker") == []

    assert (await asker.fulfill("asker", pending["request_id"], _submission()))["granted"]
    await _settle(asker)

    [wake] = _wakes("asker")
    prompt = wake["prompt"]
    [grant] = asker.grants.values()
    assert "<kind>access</kind>" in prompt and "<status>granted</status>" in prompt
    assert grant.id in prompt and pending["request_id"] in prompt
    assert "Host notice" in prompt
    # Only what Grant.model_public() exposes: never the address, label or secret.
    for private in (_SECRET, "private-example", _LABEL):
        assert private not in prompt

    # One notice per request, however often the outcome is reported.
    frame = wake["payload"]["pending_frames"][0]
    await turn_scheduler.on_access_outcome("asker", frame)
    assert len(_wakes("asker")) == 1


@pytest.mark.asyncio
async def test_running_turn_gets_the_decline_at_its_next_tool_boundary(asker, monkeypatch):
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    pending = await asker.request("asker", "service", "Read DNS records", service_provider="custom")
    await _settle(asker)

    await asker.decline("asker", pending["request_id"])
    await _settle(asker)

    assert steer.await_count == 1
    prompt = steer.await_args.args[1]
    assert "<kind>access</kind>" in prompt and "<status>declined</status>" in prompt
    assert pending["request_id"] in prompt and "without the user's consent" in prompt
    assert _wakes("asker") == []


@pytest.mark.asyncio
async def test_user_stopped_chat_gets_no_notice(asker, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "asker")
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    pending = await asker.request("asker", "service", "Read DNS records", service_provider="custom")
    await asker.decline("asker", pending["request_id"])
    await _settle(asker)
    assert steer.await_count == 0 and _wakes("asker") == []
