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


class _NullProvider:
    name = "null"

    async def stream(self, request):  # pragma: no cover — never called
        if False:
            yield None


@pytest.mark.asyncio
async def test_access_notice_is_not_a_background_task_in_the_engine(asker, monkeypatch):
    """Neither the wake turn nor the mid-turn receipt retires ``access-<id>``.

    No task by that id ever started, so a ``BackgroundTaskFinished`` for it
    would leave a ``completion_pending`` receipt in Fluid that never resolves.
    """
    from types import SimpleNamespace

    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore

    from app.core import session_pool
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    pending = await asker.request("asker", "service", "Read DNS records", service_provider="custom")
    await asker.decline("asker", pending["request_id"])
    await _settle(asker)
    [wake] = _wakes("asker")
    frames = wake["payload"]["pending_frames"]

    loop = NativeLoop(provider=_NullProvider(), store=MemoryStore(), model="m")
    await loop._ensure_session()
    adapter = HarnessAdapter()
    adapter._engine, adapter._connected = loop, True
    adapter._options = SessionOptions(model="m", extra={"app_session_id": "asker"})

    task_id = f"access-{pending['request_id']}"

    async def assert_no_receipt():
        assert task_id not in loop._background_early
        events = await loop.store.events_since(loop.session_id, -1)
        assert not [e for e in events if getattr(e, "task_id", None) == task_id]

    # Wake turn: options.aih_background_task_completions carries the frames.
    await adapter.prepare_background_completions(frames)
    await assert_no_receipt()
    # Mid-turn steer receipt: the runtime retires the steered frames.
    pool = SimpleNamespace(peek=lambda sid: SimpleNamespace(adapter=adapter))
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    await turn_scheduler.retire_task_terminals("asker", frames)
    await assert_no_receipt()


# A subagent's card shows in its main chat. Its own session never gets the
# notice (a finished child would be restarted, a delegated CLI could take it as
# a follow-up); the main chat gets a copy without the handle. Identity comes
# from the persisted rows (is_worker, parent_session_id), never ``_main``.


def _notices(session_id):
    return [f for t in _wakes(session_id) for f in (t.get("payload") or {}).get("pending_frames") or []]


def _steered(steer):
    return [call.args[0] for call in steer.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("child_live", [True, False], ids=["child-running", "child-finished"])
async def test_subagent_outcome_reaches_its_main_chat_never_the_child(asker, monkeypatch, child_live):
    from app.core import session_runtime

    chat("child", is_worker=True, parent_session_id="asker")
    if child_live:
        monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: object())
    steer = AsyncMock(return_value=False)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    pending = await asker.request("child", "service", "Read DNS records", service_provider="custom")
    await _settle(asker)
    asker._main.clear()  # the evictable UI cache must not decide who is notified

    assert (await asker.fulfill("child", pending["request_id"], _submission()))["granted"]
    await _settle(asker)

    assert "child" not in _steered(steer) and _wakes("child") == []
    [notice] = _notices("asker")
    assert notice["task_id"] == f"access-{pending['request_id']}" and notice["status"] == "granted"
    [wake] = _wakes("asker")
    [grant] = asker.grants.values()
    assert pending["request_id"] in wake["prompt"] and "subagent session child" in wake["prompt"]
    assert "cannot use it" in wake["prompt"] and "message it to continue" in wake["prompt"]
    # No handle and nothing about the resource in the main chat's copy.
    for private in (grant.id, _SECRET, "private-example", _LABEL, "expires_at", "writable"):
        assert private not in wake["prompt"]


@pytest.mark.asyncio
async def test_nested_subagent_outcome_goes_to_the_root_chat(asker, monkeypatch):
    chat("child", is_worker=True, parent_session_id="asker")
    chat("grandchild", is_worker=True, parent_session_id="child")
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", AsyncMock(return_value=False))
    pending = await asker.request("grandchild", "service", "Read DNS records", service_provider="custom")
    await asker.decline("grandchild", pending["request_id"])
    await _settle(asker)

    assert _wakes("grandchild") == [] and _wakes("child") == []
    [wake] = _wakes("asker")
    assert "subagent session grandchild" in wake["prompt"] and "<status>declined</status>" in wake["prompt"]
    assert "Do not have it requested again without the user's consent." in wake["prompt"]


@pytest.mark.asyncio
async def test_two_subagents_answers_are_two_notices_in_the_main_chat(asker, monkeypatch):
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", AsyncMock(return_value=False))
    ids = []
    for child in ("child-a", "child-b"):
        chat(child, is_worker=True, parent_session_id="asker")
        ids.append((await asker.request(child, "service", "Read DNS records", service_provider="custom"))["request_id"])
    await asker.decline("child-a", ids[0])
    assert (await asker.fulfill("child-b", ids[1], _submission()))["granted"]
    await _settle(asker)

    assert sorted(f["task_id"] for f in _notices("asker")) == sorted(f"access-{i}" for i in ids)


@pytest.mark.asyncio
async def test_stopped_main_chat_gets_no_subagent_notice(asker, monkeypatch):
    from app.core.task_registry import task_registry

    chat("child", is_worker=True, parent_session_id="asker")
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "asker")
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    pending = await asker.request("child", "service", "Read DNS records", service_provider="custom")
    await asker.decline("child", pending["request_id"])
    await _settle(asker)
    assert steer.await_count == 0 and _wakes("asker") == [] and _wakes("child") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-row", "lookup-error"])
async def test_unreadable_asker_row_notifies_nobody(asker, monkeypatch, failure):
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    asker_id = "ghost" if failure == "missing-row" else "asker"
    pending = await asker.request(asker_id, "service", "Read DNS records", service_provider="custom")
    await _settle(asker)

    def broken(session_id, *args, **kwargs):
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        if failure == "lookup-error":
            patch.setattr(database, "get_session", broken)
        # The owner's answer still succeeds; only the notice is withheld.
        assert (await asker.fulfill(asker_id, pending["request_id"], _submission()))["granted"]
        await _settle(asker)
    assert steer.await_count == 0
    assert _wakes(asker_id) == [] and _wakes("asker") == []
