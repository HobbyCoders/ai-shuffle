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
    row = {"device_id": "peer-worker", "online": True, "capabilities": {"generation": peer.WIRE_VERSION}}
    monkeypatch.setattr(runners, "self_device_id", lambda: "local-worker")
    monkeypatch.setattr(runners, "find", lambda worker: row if worker == row["device_id"] else None)
    monkeypatch.setattr(runners, "other_workers", lambda: [row])
    calls = []
    async def call(worker, action, body, **kw):
        calls.append((worker, action, body, kw))
        if body["operation"] == "catalog":
            return {"version": peer.WIRE_VERSION, "candidates": [asdict(ENTRY)]}
        if body["operation"] == "prepare":
            return {"version": peer.WIRE_VERSION, "candidate": asdict(ENTRY)}
        if body["operation"] == "execute":
            return {"version": peer.WIRE_VERSION, "result": {"success": True, "image_url": "/api/v1/artifacts/fake"}}
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


@pytest.mark.parametrize("version", [None, 0, 1, False, "2"])
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
    body = {"version": peer.WIRE_VERSION, "operation": "execute", "source": ENTRY.account, "kind": "image", "model": ENTRY.model,
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


async def test_reported_overrun_goes_to_founder_not_each_account_worker(network, monkeypatch):
    from app.network import engine, control
    monkeypatch.setattr(engine, "load_context", lambda: SimpleNamespace(founder_device_id="peer-worker", device_id="local-worker"))
    call = AsyncMock(return_value={"version": peer.WIRE_VERSION, "recorded": True})
    monkeypatch.setattr(control, "call", call)
    await peer.record_overrun("0.01")
    assert call.call_args.args[:2] == ("peer-worker", "generation")
    assert call.call_args.args[2] == {"version": peer.WIRE_VERSION, "operation": "record-overrun", "amount": "0.01"}


async def test_founder_receiver_records_actual_cost_without_authorizing_submission(monkeypatch):
    from app.network import engine
    from app.core import generation_budget
    monkeypatch.setattr(engine, "load_context", lambda: None)
    record = []
    monkeypatch.setattr(generation_budget, "record_overrun", lambda amount: record.append(amount))
    result = await peer.handle({"version": peer.WIRE_VERSION, "operation": "record-overrun", "amount": "0.01"}, "account-worker")
    assert result == {"version": peer.WIRE_VERSION, "recorded": True}
    assert record == ["0.01"]


@pytest.mark.parametrize("operation", ["catalog", "prepare", "execute"])
async def test_holding_worker_applies_its_own_plan_image_rule(monkeypatch, operation):
    # Worker B's Image generation is the ChatGPT plan: it never spends on a
    # fal image its own generateImage would refuse, whoever asks.
    from app.services import generation_sources
    from app.mcp.node_worker import NodeWorker
    from app.network import runners
    from app.core.provider import subscription_images as sub
    fal = Candidate("fal", "fal-ai", "peer-worker", "fal-ai/flux/dev", "image", False, options={}, price=PRICE)
    monkeypatch.setattr(runners, "self_device_id", lambda: "peer-worker")
    monkeypatch.setattr(generation_sources.Sources, "local_catalog", AsyncMock(return_value=[fal, ENTRY]))
    execute = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(generation_sources.Sources, "execute_local", execute)
    monkeypatch.setattr(NodeWorker, "shutdown", AsyncMock())
    monkeypatch.setattr(peer, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(peer, "claim", lambda command: True)
    monkeypatch.setattr(sub, "plan_images_configured", lambda: True)
    body = {"version": peer.WIRE_VERSION, "operation": operation, "source": "fal-ai", "kind": "image", "model": fal.model,
            "options": {}, "prompt": "tree", "price": PRICE, "session_id": "session", "command_id": "request"}
    if operation == "catalog":
        output = await peer.handle(body, "origin-worker")
        assert [c["source"] for c in output["candidates"]] == ["chatgpt"]
    else:
        with pytest.raises(ValueError, match="not generated with fal.ai"):
            await peer.handle(body, "origin-worker")
    execute.assert_not_awaited()


async def test_one_malformed_peer_reply_marks_only_that_peer_unavailable(monkeypatch):
    from app.network import runners
    monkeypatch.setattr(runners, "other_workers", lambda: [{"device_id": "p1", "online": True}, {"device_id": "p2", "online": True}])
    good = asdict(replace(ENTRY, worker="p2"))
    async def reply(worker, operation, **body):
        return {"version": peer.WIRE_VERSION, "candidates": [{"source": "fal"}] if worker == "p1" else [good]}
    monkeypatch.setattr(peer, "request", reply)
    entries = await peer.catalog("image")
    assert [(c.worker, c.available) for c in entries] == [("p1", False), ("p2", True)]
    assert "nothing from it was used" in entries[0].reason
