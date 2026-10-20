# ruff: noqa: F811
"""Private access follows local Task ancestry, not UI routing or peer chats."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.core import chat_access, chat_files
from app.core.chat_access import AccessDenied, Broker, GrantInput, GrantSubmission
from app.db import database
from tests.core.test_chat_access import _until, broker, delegation, pending, ssh_target  # noqa: F401
from tests.core.test_chat_services import pki, service, spec, trust  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def approved(delegation, tmp_path):
    if not chat_files.available():
        pytest.skip("Linux confinement required")
    broker, _ = delegation
    # Drive the producing path: owner approval, not a seeded consumer handle.
    task = asyncio.create_task(broker.request("main", "folder", "Read logs"))
    await _until(lambda: broker.requests)
    request = next(iter(broker.requests))
    await broker.fulfill("main", request, GrantSubmission(connection=GrantInput(
        kind="folder", label="Logs", path=str(tmp_path))))
    handle = await task
    yield broker, handle, tmp_path
    for grant in list(broker.grants.values()):
        broker.revoke(grant.session_id, grant.id)
    await asyncio.gather(*broker._notifications)


@pytest.mark.parametrize("child", ["child", "nested"])
async def test_approved_parent_handle_is_usable_by_descendants(approved, child):
    broker, handle, path = approved
    (path / "log").write_text("ready")
    assert (await broker.resources(child))["grants"] == [handle]
    assert await broker.run(child, handle["id"], "read", {"path": "log"}) == {"content": "ready"}
    assert len(broker.grants) == 1  # no copied credentials, expiry, timer or lease
    assert broker.grants[handle["id"]].session_id == "main"


@pytest.mark.parametrize("other", ["peer", "orphan", "loop-a", "remote-child"])
async def test_unrelated_and_invalid_chains_cannot_claim_parent_handle(approved, other):
    broker, handle, _ = approved
    assert (await broker.resources(other))["grants"] == []
    with pytest.raises(AccessDenied):
        await broker.get(other, handle["id"])
    assert await broker.get("main", handle["id"])  # refusal cannot revoke another session's grant


async def test_direct_caller_not_flattened_routing_parent_controls_inheritance(approved):
    broker, _, path = approved
    # Real nested Task rows name the root for card routing. dispatch_worker
    # registers the actual caller separately before starting the child.
    database.get_session("nested")["parent_session_id"] = "main"
    broker.register_delegation("nested", "child", routing_parent="main")
    own = await broker.create("child", GrantInput(kind="folder", label="Child", path=str(path)))
    assert own["id"] in {g["id"] for g in (await broker.resources("nested"))["grants"]}
    for other in ("main", "peer"):
        with pytest.raises(AccessDenied):
            await broker.get(other, own["id"])
    # Another child of the same main chat is not this child's descendant.
    broker._parents.pop("nested")
    with pytest.raises(AccessDenied):
        await broker.get("nested", own["id"])


@pytest.mark.parametrize("end", ["revoke", "expire"])
async def test_parent_end_cascades_without_regrant_or_renewal(approved, end):
    broker, handle, _ = approved
    if end == "revoke":
        broker.revoke("main", handle["id"])
    else:
        broker.grants[handle["id"]].expires_at = time.time() - 1
    for child in ("child", "nested"):
        with pytest.raises(AccessDenied):
            await broker.get(child, handle["id"])
        assert (await broker.resources(child))["grants"] == []
    assert not broker.grants
    assert (await Broker().resources("child"))["grants"] == []  # restart still revokes


async def test_child_cannot_widen_scope_or_manage_parent_grant(approved):
    broker, handle, _ = approved
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run("child", handle["id"], "write", {"path": "new", "content": "no"})
    with pytest.raises(AccessDenied):
        await broker.run("nested", handle["id"], "read", {"path": "../outside"})
    with pytest.raises(AccessDenied):
        broker.revoke("child", handle["id"])
    with pytest.raises(AccessDenied):
        await broker.get("child", handle["id"], preparing=True)
    assert not broker.actions
    assert await broker.get("main", handle["id"])


@pytest.mark.parametrize("decision", ["deny", "revoke", "approve", "child-bypass"])
async def test_action_uses_child_mode_and_child_approval(approved, monkeypatch, decision):
    broker, handle, path = approved
    grant = broker.grants[handle["id"]]
    grant.writable = True
    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", AsyncMock(
        side_effect=lambda sid: sid == "main" or (decision == "child-bypass" and sid == "child")))
    running = asyncio.create_task(broker.run("child", grant.id, "write", {"path": "new", "content": "yes"}))
    if decision == "child-bypass":
        assert await running == {"written": True}
        assert not broker.actions
        return
    action = await pending(broker)
    assert action.session_id == "child" and action.grant_id == grant.id
    [shown] = (await broker.snapshot("main"))["actions"]
    assert shown["session_id"] == "child"
    with pytest.raises(AccessDenied):
        await broker.decide("main", action.id, True)
    if decision == "revoke":
        broker.revoke("main", grant.id)
    else:
        await broker.decide("child", action.id, decision == "approve")
    if decision == "approve":
        assert await running == {"written": True}
    else:
        with pytest.raises(AccessDenied):
            await running
        assert not (path / "new").exists()


async def test_inherited_ssh_run_is_cancelled_by_parent_revocation(delegation, ssh_target):
    broker, _ = delegation
    spec, commands, started = ssh_target
    task = asyncio.create_task(broker.request("main", "machine", "Inspect server"))
    await _until(lambda: broker.requests)
    await broker.fulfill("main", next(iter(broker.requests)), GrantSubmission(connection=spec))
    handle = await task
    running = asyncio.create_task(broker.run("nested", handle["id"], "command", {"command": "hold"}))
    action = await pending(broker)
    assert action.session_id == "nested"
    await broker.decide("nested", action.id, True)
    await asyncio.wait_for(started.wait(), 3)
    broker.revoke("main", handle["id"])
    with pytest.raises(AccessDenied):
        await running
    assert commands == ["hold"]
    assert not broker.grants
    await asyncio.gather(*broker._notifications)


async def test_inherited_https_uses_same_readonly_scope_and_private_credential(delegation, service, trust):
    broker, _ = delegation
    request = await broker.request("main", "service", "Read inventory")
    await broker.fulfill("main", request["request_id"], GrantSubmission(connection=spec(service)))
    grant = next(iter(broker.grants.values()))
    result = await broker.run("child", grant.id, "http", {"method": "GET", "path": "/inventory"})
    assert result["status"] == 200
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run("nested", grant.id, "http", {"method": "POST", "path": "/inventory"})
    assert len(service.requests) == 1
    broker.revoke("main", grant.id)
    await asyncio.gather(*broker._notifications)


async def test_registered_link_revalidates_persisted_routing_parent(approved):
    broker, handle, _ = approved
    broker.register_delegation("child", "main")
    database.get_session("child")["parent_session_id"] = "peer"
    with pytest.raises(AccessDenied):
        await broker.get("nested", handle["id"])
    assert (await broker.resources("child"))["grants"] == []


async def test_principal_change_invalidates_descendant_access(approved):
    broker, handle, _ = approved
    database.get_session("child")["api_user_id"] = "someone-else"
    assert (await broker.resources("nested"))["grants"] == []
    with pytest.raises(AccessDenied):
        await broker.get("child", handle["id"])
    assert await broker.get("main", handle["id"])
