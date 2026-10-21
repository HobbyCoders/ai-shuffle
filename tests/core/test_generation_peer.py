"""Cross-account worker routing uses authenticated, versioned peer controls."""
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation_resolver import Candidate, Resolver, RunContext
from app.network import generation as peer
from app.services.generation_sources import Sources

PRICE = {"currency": "USD", "unit": "item", "amount": "0.05", "total": "0.05", "status": "estimated"}
ENTRY = Candidate("chatgpt", "remote-account", "peer-worker", "default", "image", True,
                  options={"size": "auto", "quality": "auto", "format": "png"}, price=PRICE)
CONTEXT = RunContext(session_id="session", owner=True, unattended=False)


@pytest.fixture
def network(monkeypatch):
    from app.network import runners, control
    row = {"device_id": "peer-worker", "online": True, "capabilities": {"generation": 1}}
    monkeypatch.setattr(runners, "self_device_id", lambda: "local-worker")
    monkeypatch.setattr(runners, "find", lambda worker: row if worker == row["device_id"] else None)
    monkeypatch.setattr(runners, "other_workers", lambda: [row])
    calls = []
    async def call(worker, action, body, **kw):
        calls.append((worker, action, body, kw))
        if body["operation"] == "catalog":
            return {"version": 1, "candidates": [asdict(ENTRY)]}
        if body["operation"] == "prepare":
            return {"version": 1, "candidate": asdict(ENTRY)}
        if body["operation"] == "execute":
            return {"version": 1, "result": {"success": True, "image_url": "/api/v1/artifacts/fake"}}
        raise AssertionError(body)
    monkeypatch.setattr(control, "call", call)
    return row, calls


async def test_explicit_remote_account_resolves_and_runs_only_on_holding_worker(monkeypatch, network):
    _, calls = network
    source = Sources(SimpleNamespace(call=AsyncMock()), CONTEXT)
    monkeypatch.setattr(source, "local_catalog", AsyncMock(return_value=[]))
    output = await Resolver(source).generate("image", {"prompt": "tree", "source": ENTRY.account}, CONTEXT)
    assert output["status"] == "completed"
    assert output["choice"]["worker"] == ENTRY.worker
    assert [body["operation"] for _, _, body, _ in calls] == ["catalog", "prepare", "execute"]
    assert all(worker == ENTRY.worker and action == "generation" for worker, action, _, _ in calls)
    source.node.call.assert_not_awaited()
    assert calls[-1][2]["session_id"] == CONTEXT.session_id
    assert calls[-1][2]["source"] == ENTRY.account
    assert "command_id" in calls[-1][2]
    assert calls[-1][2]["price"] == PRICE


@pytest.mark.parametrize("version", [None, 0, False, "1"])
async def test_older_peer_is_refused_before_any_dispatch(network, version):
    row, calls = network
    row["capabilities"] = {} if version is None else {"generation": version}
    with pytest.raises(ValueError, match="Update that worker"):
        await peer.request(ENTRY.worker, "execute")
    assert calls == []
    entries = await peer.catalog("image", source=ENTRY.account)
    assert not entries[0].available
    assert "Update that worker" in entries[0].reason


async def test_older_protocol_request_is_refused_on_receiver():
    with pytest.raises(ValueError, match="Update that worker"):
        await peer.handle({"version": 0, "operation": "execute"}, "peer-worker")


async def test_changed_or_old_peer_reply_is_refused(network, monkeypatch):
    from app.network import control
    monkeypatch.setattr(control, "call", AsyncMock(return_value={"version": 0}))
    with pytest.raises(ValueError, match="Update that worker"):
        await peer.request(ENTRY.worker, "catalog")


async def test_ambiguous_network_failure_never_submits_again(network, monkeypatch):
    from app.network import control
    call = AsyncMock(side_effect=control.MaybeApplied("timeout"))
    monkeypatch.setattr(control, "call", call)
    with pytest.raises(ValueError, match="Do not automatically retry"):
        await peer.request(ENTRY.worker, "execute")
    call.assert_awaited_once()


async def test_peer_receiver_rechecks_options_price_budget_and_claim(monkeypatch):
    from app.services import generation_sources
    from app.mcp.node_worker import NodeWorker
    from app.network import runners
    monkeypatch.setattr(runners, "self_device_id", lambda: ENTRY.worker)
    monkeypatch.setattr(generation_sources.Sources, "local_catalog", AsyncMock(return_value=[ENTRY]))
    monkeypatch.setattr(generation_sources.Sources, "prepare", AsyncMock(return_value=ENTRY))
    execute = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(generation_sources.Sources, "execute_local", execute)
    monkeypatch.setattr(NodeWorker, "shutdown", AsyncMock())
    reserve = AsyncMock(return_value=False)
    monkeypatch.setattr(peer, "reserve", reserve)
    monkeypatch.setattr(peer, "claim", lambda command: True)
    body = {"version": 1, "operation": "execute", "source": ENTRY.account, "kind": "image", "model": ENTRY.model,
            "options": ENTRY.options, "prompt": "tree", "price": PRICE, "session_id": "session", "command_id": "request"}
    output = await peer.handle(body, "origin-worker")
    assert not output["result"]["success"]
    execute.assert_not_awaited()
    reserve.return_value = True
    output = await peer.handle(body, "origin-worker")
    assert output["result"]["success"]
    execute.assert_awaited_once()
    with pytest.raises(ValueError, match="price changed"):
        await peer.handle({**body, "price": {**PRICE, "total": "0"}}, "origin-worker")
    execute.assert_awaited_once()
