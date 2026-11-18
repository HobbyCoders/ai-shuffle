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
    for changed in ("0", "0.04", "0.06"):
        with pytest.raises(ValueError, match="price changed"):
            await peer.handle({**body, "price": {**PRICE, "total": changed}}, "origin-worker")
    execute.assert_awaited_once()
    # The same money written differently ("0.050") is the same price.
    output = await peer.handle({**body, "price": {**PRICE, "amount": "0.050", "total": "0.0500"}}, "origin-worker")
    assert output["result"]["success"] and execute.await_count == 2


@pytest.mark.parametrize("operation,flag,listing", [
    ("catalog", None, True), ("catalog", False, False), ("prepare", None, False), ("execute", None, False)])
async def test_the_peer_skips_listing_rates_for_the_candidate_it_prices_fresh(monkeypatch, operation, flag, listing):
    # prepare/execute price the one candidate fresh; reading listing rates first
    # would queue admission behind a catalog page on the rate cache's lock.
    from app.services import generation_sources
    local = AsyncMock(return_value=[])
    monkeypatch.setattr(generation_sources.Sources, "local_catalog", local)
    body = {"version": peer.WIRE_VERSION, "operation": operation, "source": ENTRY.account, "kind": "image",
            "model": ENTRY.model, **({} if flag is None else {"listing_prices": flag})}
    if operation == "catalog":
        await peer.handle(body, "origin-worker")
    else:
        with pytest.raises(ValueError, match="No unique"):
            await peer.handle(body, "origin-worker")
    assert local.await_args.kwargs["listing_prices"] is listing


async def test_a_named_remote_lookup_asks_the_peer_not_to_read_listing_rates(network):
    _, calls = network
    await peer.catalog("image", source=ENTRY.account, model=ENTRY.model, listing_prices=False)
    await peer.catalog("image", source=ENTRY.account)
    assert calls[0][2]["listing_prices"] is False and "listing_prices" not in calls[1][2]


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


async def test_holding_worker_serves_pages_for_an_operation(monkeypatch):
    from app.services import generation_sources
    from app.mcp.node_worker import NodeWorker
    from app.network import runners
    fal = Candidate("fal", "fal-ai", "peer-worker", "fal-ai/i2v", "video", False, options={}, price=PRICE)
    monkeypatch.setattr(runners, "self_device_id", lambda: "peer-worker")
    page = AsyncMock(return_value=([fal], 9, {"fal-ai/i2v": {"name": "I2V"}}))
    monkeypatch.setattr(generation_sources.Sources, "fal_page", page)
    monkeypatch.setattr(NodeWorker, "shutdown", AsyncMock())
    body = {"version": peer.WIRE_VERSION, "operation": "catalog", "kind": "video", "source": "fal",
            "media_operation": "image-to-video", "page": {"offset": 20, "limit": 20}, "query": "kling"}
    output = await peer.handle(body, "origin-worker")
    assert output["page"] == {"total": 9, "offset": 20, "details": {"fal-ai/i2v": {"name": "I2V"}}, "serves": True}
    assert output["media_operation"] == "image-to-video"
    assert page.await_args.kwargs == {"operation": "image-to-video", "query": "kling", "offset": 20, "limit": 20}
    # A worker without fal for this kind says so instead of sending an empty page.
    page.return_value = None
    output = await peer.handle(body, "origin-worker")
    assert output["page"] == {"total": 0, "offset": 20, "details": {}, "serves": False} and output["candidates"] == []
    page.return_value = ([], 0, {})
    assert (await peer.handle(body, "origin-worker"))["page"]["serves"] is True
    with pytest.raises(ValueError, match="Unknown generation operation"):
        await peer.handle({**body, "media_operation": "rm -rf"}, "origin-worker")
    with pytest.raises(ValueError, match="page"):
        await peer.handle({**body, "page": {"offset": -1, "limit": 20}}, "origin-worker")


async def test_older_peer_text_rows_are_not_offered_for_another_operation(monkeypatch):
    from app.network import runners
    monkeypatch.setattr(runners, "other_workers", lambda: [{"device_id": "p1", "online": True}])
    fal = asdict(Candidate("fal", "fal-ai", "p1", "fal-ai/t2v", "video", False, price=PRICE))
    plan = asdict(replace(ENTRY, worker="p1", kind="video"))
    sent = []

    async def old_peer(worker, operation, **body):
        sent.append(body)
        return {"version": peer.WIRE_VERSION, "candidates": [fal, plan]}  # no media_operation echo
    monkeypatch.setattr(peer, "request", old_peer)
    assert await peer.catalog("video", operation="image-to-video") == []
    assert sent[-1]["media_operation"] == "image-to-video"
    # Its text rows still serve a text call, and a named model of any category.
    assert [c.source for c in await peer.catalog("video")] == ["fal", "chatgpt"]
    assert len(await peer.catalog("video", operation="image-to-video", model="fal-ai/t2v")) == 2
