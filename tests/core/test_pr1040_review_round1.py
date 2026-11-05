"""Producing-path regressions from the fresh independent PR 1040 review."""
import asyncio
from pathlib import Path
import threading
from unittest.mock import AsyncMock

import pytest
from fluid.tools.registry import ToolCtx
from app.core import model_override as mo, subagent_default, task_dispatch as td
from app.core import provider_agent, query_options, swarm, turn_manager, turn_scheduler, worker_checkout
from app.core.config import settings
from app.db import database, turns
from app.db.base import get_db
from tests.core.test_model_override import chats, world, _native_call, _native_request, _turn  # noqa: F401
from tests.core.test_model_override_memory import _save
from tests.core.test_backend_followup_regressions import bind_worker
from tests.core.test_worker_checkout import repo, git  # noqa: F401
from tests.shared_fixtures import foreign_keys_off, pin_runner, clean_task_registry  # noqa: F401


@pytest.mark.parametrize("surface", ["start", "task", "swarm"])
@pytest.mark.parametrize("wording_kind", ["plain", "version", "oversized"])
async def test_native_same_baseline_still_checks_qualification(chats, monkeypatch, surface, wording_kind):
    entry = "start" if surface == "start" else "task"
    sid = _native_request(chats, entry, "owner", "Start a review")
    subagent_default.write("opus")
    database.update_profile("native-retry", config={"provider_models": {"__native__": "opus"}})
    database.set_session_run_binding("caller", model="opus")
    if wording_kind != "plain":
        _save("Use Opus for reviews.")
        _turn(sid, "user", "Use Opus version 4" + (" x" * 33000 if wording_kind == "oversized" else ""))
    if surface != "swarm":
        ok, result = await _native_call(entry, "opus", "same-baseline")
    else:
        for name in ("coordinators", "workers", "worker_jobs", "_locks"):
            monkeypatch.setattr(swarm, name, {})
        monkeypatch.setattr(swarm, "persist", AsyncMock())
        monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
        monkeypatch.setattr(td, "_await_worker_registration", AsyncMock(return_value=True))
        async def worker(**kwargs):
            chats[1].append(kwargs)
            wid = kwargs["worker_session_id"]
            await provider_agent._notify_caller_terminal("parent", wid, wid, "completed", "done")
            return provider_agent.WorkerOutcome(ok=True, session_id=wid, report="done")
        monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
        ctx = ToolCtx(extra={"task_dispatch": td.make_fluid_dispatcher("parent", "caller", None)})
        outcome = await swarm.make_handler(lambda: "parent")({"action": "start", "tasks": [
            {"id": "review", "agent": "native-retry", "prompt": "go", "model": "opus"}]}, ctx)
        assert not outcome.is_error, outcome.text
        coordinator = swarm.coordinators["parent"]
        key = outcome.background_task.task_id
        await coordinator.jobs[key]
        result = coordinator.snapshot(key)
        ok = result["status"] == "completed"
        from app.core.task_registry import task_registry
        task_registry.clear_session("parent")
    assert ok is (wording_kind == "plain"), result
    assert len(chats[2]) == int(wording_kind != "plain")


async def test_confirm_wrapper_handles_unchanged_native_baseline(chats):
    _native_request(chats, "start", "owner", "Start a review")
    assert await mo.confirm("parent", "caller", model="opus", baseline="opus", action="start") is None
    assert not chats[2]


@pytest.mark.parametrize("entry", ["start", "task"])
@pytest.mark.parametrize("source", ["owner", "memory"])
@pytest.mark.parametrize("wording", ["Use Opus at version 4", "Use Opus with version four"])
async def test_labeled_version_after_gap_requires_agreement(chats, entry, source, wording):
    _native_request(chats, entry, source, wording)
    ok, error = await _native_call(entry, "opus", "gap-version")
    assert not ok and len(chats[2]) == 1 and not chats[1], error


@pytest.mark.parametrize("source", ["user", "api"])
async def test_owner_enqueue_preflight_waits_for_old_finisher(repo, monkeypatch, source):
    checkout = bind_worker(repo)
    entered, release = threading.Event(), threading.Event()
    reclaim = worker_checkout.reclaim_untouched_worker_checkout
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return reclaim(*args)
    monkeypatch.setattr(worker_checkout, "reclaim_untouched_worker_checkout", delayed)
    finisher = asyncio.create_task(worker_checkout.reclaim_finished_worker_checkout("followup"))
    assert await asyncio.to_thread(entered.wait, 2)
    preflight = None
    try:
        # Real owner/API intake producer, not a seeded busy predicate.
        turn_id = await turn_manager.submit_async("followup", source, prompt="continue", profile_id="coder", no_kick=True)
        turns.start_turn(turn_id)
        preflight = asyncio.create_task(query_options.ensure_pending_checkout(database.get_session("followup")))
        await asyncio.sleep(0.05)
    finally:
        release.set()
    await finisher
    await preflight
    assert turns.session_has_unsettled_turns("followup")
    assert Path(checkout.path).is_dir(), "old finisher removed the cwd after owner execution preflight"
    assert git(checkout.path, "branch", "--show-current") == checkout.branch


async def test_zero_output_retry_cannot_reclaim_a_queued_followup(repo, monkeypatch):
    checkout = bind_worker(repo)
    await turn_manager.submit_async("followup", "user", prompt="continue", profile_id="coder", no_kick=True)
    monkeypatch.setattr(settings, "task_worker_start_retries", 1)
    monkeypatch.setattr(td, "_retry_backoff", lambda *a: 0)
    monkeypatch.setattr(td, "_reserve_admission", AsyncMock(return_value=("stop after cleanup probe", False)))
    monkeypatch.setattr(td, "_release_admission", lambda *a: None)
    monkeypatch.setattr(swarm, "bind_worker", AsyncMock())
    async def failed(**kwargs):
        return provider_agent.WorkerOutcome(ok=False, session_id="followup", error="transport failed before connect", frames_forwarded=0)
    async with turn_scheduler.message_resume_lock("followup"):
        retry = asyncio.create_task(td._dispatch_worker_with_retries(
            kwargs={"worker_session_id": "followup", "context_session_id": "parent"},
            background=True, lane="background", provider_id=None, parent_session_id="parent", dispatcher=failed))
        await asyncio.sleep(0.05)
    await retry
    assert turns.session_has_unsettled_turns("followup")
    assert Path(checkout.path).is_dir(), "automatic retry reclaimed a checkout whose followup was already queued"
