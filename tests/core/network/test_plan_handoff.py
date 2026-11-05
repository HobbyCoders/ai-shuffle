"""Produce plans, then use the real session-move export/stage/publication path."""
import json

import pytest
from app.core.config import settings
from app.core.session_plan import Backend
from app.db import database, runs, session_plans, session_events
from app.network import handoff
from fluid.tools.builtins import plan
from fluid.tools.registry import ToolCtx
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401
from tests.core.network.test_handoff_folders import SID, _chat, _move
from tests.core.network.harness import converge


async def produce(worker, args):
    with worker.active():
        run = runs.claim_run(SID, settings.effective_instance_id, 120)
        assert run
        proof = (SID, run["id"], run["fence_token"])
        ctx = ToolCtx(cwd=str(worker.data_dir), session_id="unrelated-sdk")
        ctx.extra["session_plan"] = Backend(lambda: SID, lambda: proof)
        try:
            outcome = await plan.handler(args, ctx)
            assert not outcome.is_error, outcome.text
            return json.loads(outcome.text), ctx
        finally:
            runs.release_run(run["id"], run["fence_token"])


async def test_roundtrip_replaces_stale_replica_and_preserves_receipts_and_history(pair, monkeypatch, tmp_path):
    a, b, _, _ = pair
    _chat(a, b)
    # The lightweight pair fixture advertises a legacy capability set. Inject
    # only the newly supported transport capability, not any plan state.
    from app.network import runners
    original_find = runners.find
    def capable(target):
        row = original_find(target) or {}
        return {**row, "capabilities": {**(row.get("capabilities") or {}), "session_plan": 1}}
    monkeypatch.setattr(runners, "find", capable)
    first = {"action": "update", "expected_revision": 0, "request_id": "first",
             "goal": "First goal", "done_when": "Accepted by owner",
             "tasks": [{"id": "one", "title": "Wait for review", "status": "blocked", "blocked_reason": "Review unavailable"}]}
    _, stale_ctx = await produce(a, first)
    assert await _move("api", a, b, monkeypatch, tmp_path) is None
    with b.active():
        assert session_plans.read(SID)["plan"]["tasks"][0]["blocked_reason"] == "Review unavailable"
    replay, _ = await produce(b, first)
    assert replay["replayed"] and replay["revision"] == 1
    await produce(b, {"action": "update", "expected_revision": 1, "request_id": "review-ready",
                     "tasks": [{"id": "one", "title": "Review", "status": "in_progress"}]})
    converge([a, b])
    assert await _move("api", b, a, monkeypatch, tmp_path) is None
    with a.active():
        assert session_plans.read(SID)["plan"]["revision"] == 2
    await produce(a, {"action": "update", "expected_revision": 2, "request_id": "done",
                     "tasks": [{"id": "one", "title": "Review", "status": "completed"}]})
    rollover = {**first, "rollover": True, "expected_revision": 3, "request_id": "second-goal", "goal": "Second goal"}
    await produce(a, rollover)
    assert await _move("api", a, b, monkeypatch, tmp_path) is None
    with b.active():
        current = session_plans.read(SID)["plan"]
        assert current["revision"] == 4 and current["generation"] == 2
        assert current["goal"] == "Second goal"
        revisions = [e["payload"]["plan"]["revision"] for e in session_events.get_events(SID)
                     if e["event_type"] == "plan.updated"]
        assert revisions == [1, 2, 3, 4]
    replay, _ = await produce(b, rollover)
    assert replay["replayed"] and replay["revision"] == 4
    with a.active():
        rejected = await plan.handler({"action": "update", "expected_revision": 4, "request_id": "z"}, stale_ctx)
        assert rejected.is_error and "another worker" in rejected.text
    # Both handoff tables use the app id, not a newly allocated SDK id.
    assert "session_plans" in handoff.APP_TABLES

async def test_cold_mirror_projects_produced_event_without_owning_mutable_plan(pair, monkeypatch):
    from tests.core.network.harness import converge
    a, b, *_ = pair
    _chat(a, b)
    await produce(a, {"action": "update", "expected_revision": 0, "request_id": "project",
                      "goal": "Mirrored goal", "done_when": "Readable", "tasks": [{"id": "p", "title": "One", "status": "pending"}]})
    from app.network import catalog
    with a.active():
        catalog.publish_events()
    converge([a, b])
    with b.active():
        assert session_plans.read(SID)["plan"]["revision"] == 0
        view = session_plans.projection(SID)
        assert view["plan"]["revision"] == 1 and view["plan"]["goal"] == "Mirrored goal"


async def test_older_receiver_cannot_silently_drop_plan(pair, monkeypatch, tmp_path):
    a, b, *_ = pair
    _chat(a, b)
    await produce(a, {"action": "update", "expected_revision": 0, "request_id": "keep",
                      "goal": "Keep work", "done_when": "Never lost", "tasks": [{"id": "p", "title": "One", "status": "pending"}]})
    monkeypatch.setattr(handoff, "plan_carry", lambda target: False)
    refusal = await _move("api", a, b, monkeypatch, tmp_path)
    assert "Update the target worker" in refusal
    with a.active():
        assert session_plans.read(SID)["plan"]["revision"] == 1
        assert database.get_session(SID)["owner_device_id"] == a.identity.device_id

