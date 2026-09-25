"""Reproduce a local-only resume ID disappearing at the sync boundary."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import commands, rewind_owner
from app.db import database, session_events
from app.network import catalog, control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_transport import ServedBy
from tests.core.test_fluid_transcript import _seed, load_fluid_path


@pytest.fixture
async def pair(workers, monkeypatch):
    owner, mirror = workers("owner"), workers("mirror")
    with owner.active():
        engine.create_network("Home", device_name="Owner")
    join(owner, mirror)
    with owner.active():
        database.create_session("s-rewind", "default", title="Remote rewind")
        sdk = _seed()
        database.update_session("s-rewind", sdk_session_id=sdk, v2_stream=True)
        # Real catalog producer mirrors the session and events but deliberately
        # omits the provider id and node tree. Do not seed a NULL id on mirror.
        session_events.append_events("s-rewind", [
            {"type": "message.user", "payload": {"content": "first prompt"}},
            {"type": "message.assistant", "payload": {"content": "first reply", "kind": "text"}},
            {"type": "message.user", "payload": {"content": "second prompt"}},
            {"type": "message.assistant", "payload": {"content": "second reply", "kind": "text"}},
        ])
        catalog.publish_owned()
    converge([owner, mirror])
    served = ServedBy(owner, "http://owner.local:8000")
    real_async = httpx.AsyncClient
    def client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    from app.network import transport
    transport._links[owner.device_id] = transport.PeerLink(owner.device_id, [served.base_url])
    pool = MagicMock()
    pool.contains.return_value = False
    monkeypatch.setattr(commands, "get_session_pool", lambda: pool)
    monkeypatch.setattr(commands.sync_engine, "broadcast_session_rewound", AsyncMock())
    return owner, mirror, sdk, served


async def test_synced_mirror_rewinds_real_owner_and_replays_signed_event(pair):
    owner, mirror, sdk, served = pair
    with mirror.active():
        row = database.get_session("s-rewind")
        assert row["sdk_session_id"] is None
        assert row["owner_device_id"] == owner.device_id
        assert load_fluid_path(sdk) == []
        listing = await commands.get_rewind_checkpoints("s-rewind")
        assert listing.success and [c.full_message for c in listing.checkpoints if c.role == "user"] == ["first prompt", "second prompt"]
        result = await commands.execute_rewind("s-rewind", commands.RewindRequestV2(
            target_content="second prompt", target_occurrence=0, include_response=False,
        ))
        assert result.success and result.chat_rewound
        # The normal signed sync path already delivered the cut before the UI
        # reloads, without ever copying/reconstructing a provider pointer here.
        assert session_events.get_events("s-rewind")[-1]["event_type"] == "transcript.rewound"
        assert [r["payload"]["content"] for r in commands._load_message_events("s-rewind")] == ["first prompt", "first reply"]
        assert database.get_session("s-rewind")["sdk_session_id"] is None
    with owner.active():
        assert database.get_session("s-rewind")["sdk_session_id"] == sdk
        assert load_fluid_path(sdk)[-1]["node_id"] == "n5"
    assert ("POST", "/api/v1/network/peer/control/rewind") in served.requests


@pytest.mark.parametrize("operation", ["checkpoints", "execute"])
async def test_offline_owner_never_changes_mirror(pair, monkeypatch, operation):
    owner, mirror, sdk, served = pair
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.RunnerUnreachable("offline")))
    with mirror.active():
        before = session_events.get_events("s-rewind")
        with pytest.raises(HTTPException) as exc:
            if operation == "checkpoints":
                await commands.get_rewind_checkpoints("s-rewind")
            else:
                await commands.execute_rewind("s-rewind", commands.RewindRequestV2(target_content="second prompt", target_occurrence=0))
        assert exc.value.status_code == 503
        assert "owns this session" in exc.value.detail
        assert session_events.get_events("s-rewind") == before
    with owner.active():
        assert load_fluid_path(sdk)[-1]["node_id"] == "n7"


async def test_remote_ownership_wins_over_stale_local_resume_id(pair):
    owner, mirror, sdk, served = pair
    with mirror.active():
        database.update_session("s-rewind", sdk_session_id="stale-local-id")
        result = await commands.get_rewind_checkpoints("s-rewind")
        assert result.success and result.sdk_session_id == sdk


async def test_owner_rejects_requests_for_a_mirror(pair):
    owner, mirror, sdk, served = pair
    with mirror.active(), pytest.raises(PermissionError):
        await control.handle("rewind", {"session_id": "s-rewind", "operation": "execute", "params": {}}, from_device=owner.device_id)


async def test_access_checked_before_forwarding(pair, monkeypatch):
    owner, mirror, sdk, served = pair
    def denied(*args):
        raise HTTPException(403, "not yours")
    monkeypatch.setattr(commands, "check_session_access", denied)
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    with mirror.active():
        with pytest.raises(HTTPException):
            await commands.get_rewind_checkpoints("s-rewind", http_request=object())
        with pytest.raises(HTTPException):
            await commands.execute_rewind("s-rewind", commands.RewindRequestV2(), http_request=object())
    call.assert_not_awaited()


async def test_replication_failure_does_not_repeat_successful_rewind(pair, monkeypatch):
    owner, mirror, sdk, served = pair
    monkeypatch.setattr(rewind_owner, "_sync_rewind", AsyncMock(side_effect=RuntimeError("offline")))
    with mirror.active():
        result = await commands.execute_rewind("s-rewind", commands.RewindRequestV2(target_uuid="n6", include_response=False))
        assert result.success and "Do not retry" in result.warning
    assert served.requests.count(("POST", "/api/v1/network/peer/control/rewind")) == 1
    with owner.active():
        assert load_fluid_path(sdk)[-1]["node_id"] == "n5"


async def test_publication_failure_reports_completed_owner_rewind(pair, monkeypatch):
    owner, mirror, sdk, served = pair
    def fail(*args, **kwargs):
        raise RuntimeError("publish offline")
    monkeypatch.setattr(catalog, "hydrate", fail)
    with mirror.active():
        result = await commands.execute_rewind("s-rewind", commands.RewindRequestV2(target_uuid="n6", include_response=False))
        assert result.success and "publishing" in result.warning
    with owner.active():
        assert load_fluid_path(sdk)[-1]["node_id"] == "n5"


@pytest.mark.parametrize("body", [
    {"operation": "other"},
    {"operation": "execute", "params": []},
    {"operation": "execute", "params": {"target_occurrence": "invalid"}},
])
async def test_invalid_control_body_rejected_without_mutation(pair, body):
    owner, mirror, sdk, served = pair
    with owner.active(), pytest.raises(ValueError):
        await control.handle("rewind", {"session_id": "s-rewind", **body}, from_device=mirror.device_id)
    with owner.active():
        assert load_fluid_path(sdk)[-1]["node_id"] == "n7"


async def test_older_owner_refusal_is_not_a_local_fallback(pair, monkeypatch):
    owner, mirror, sdk, served = pair
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.ControlRefused("Unknown control action")))
    with mirror.active(), pytest.raises(HTTPException) as exc:
        await commands.get_rewind_checkpoints("s-rewind")
    assert exc.value.status_code == 409
