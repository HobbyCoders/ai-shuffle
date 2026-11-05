"""Producing-path regressions from the fresh independent PR 1040 review."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
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


@pytest.mark.parametrize("entry", ["start", "task"])
@pytest.mark.parametrize("source", ["owner", "memory"])
@pytest.mark.parametrize("baseline", ["sonnet", "opus"])
@pytest.mark.parametrize("wording", ["Use Opus at version: 4", "Use Opus with version: four"])
async def test_punctuated_version_label_requires_fresh_agreement(chats, entry, source, baseline, wording):
    _native_request(chats, entry, source, wording)
    subagent_default.write(baseline)
    database.update_profile("native-retry", config={"provider_models": {"__native__": baseline}})
    database.set_session_run_binding("caller", model=baseline)
    ok, error = await _native_call(entry, "claude-opus-4-7", "punctuated-pin")
    assert not ok and not chats[1] and not chats[2], error
    ok, error = await _native_call(entry, "opus", "punctuated-family")
    assert not ok and len(chats[2]) == 1 and not chats[1], error


@pytest.mark.parametrize("entry", ["start", "task"])
@pytest.mark.parametrize("source", ["owner", "memory"])
@pytest.mark.parametrize("baseline", ["sonnet", "opus"])
async def test_unlabeled_agent_count_is_still_plain_consent(chats, entry, source, baseline):
    _native_request(chats, entry, source, "Use Opus with 2 agents")
    subagent_default.write(baseline)
    database.update_profile("native-retry", config={"provider_models": {"__native__": baseline}})
    database.set_session_run_binding("caller", model=baseline)
    ok, result = await _native_call(entry, "opus", "unqualified-agent-count")
    assert ok and not chats[2], result


@pytest.mark.parametrize("entry", ["start", "task"])
@pytest.mark.parametrize("baseline", ["sonnet", "opus"])
async def test_new_plain_owner_request_overrides_qualified_saved_memory(chats, entry, baseline):
    sid = _native_request(chats, entry, "memory", "Use Opus at version: 4")
    subagent_default.write(baseline)
    database.update_profile("native-retry", config={"provider_models": {"__native__": baseline}})
    database.set_session_run_binding("caller", model=baseline)
    _turn(sid, "user", "Use Opus for this review", profile="agent" if entry == "start" else "caller")
    ok, result = await _native_call(entry, "opus", "newer-plain-owner")
    assert ok and not chats[2], result


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


@pytest.mark.parametrize("dirty", [False, True])
async def test_reclaimed_checkout_retires_pooled_cwd_generation(repo, monkeypatch, dirty):
    from app.core.session_pool import get_session_pool

    session_pool = get_session_pool()
    checkout = bind_worker(repo)
    if dirty:
        Path(checkout.path, "keep-owner-work.txt").write_text("retain this checkout")
    async def retire(session_id, *, preserve_session):
        assert session_id == "followup" and preserve_session is True
        assert not Path(checkout.path).exists()
        assert turn_scheduler.message_resume_lock(session_id).locked()
    removed = AsyncMock(side_effect=retire)
    monkeypatch.setattr(session_pool, "remove", removed)
    assert await worker_checkout.reclaim_finished_worker_checkout("followup") is not dirty
    assert removed.await_count == int(not dirty)


@pytest.mark.parametrize("supplied_checkout", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
async def test_partial_ref_cleanup_retires_only_a_removed_real_process_cwd(repo, monkeypatch, supplied_checkout, dirty):
    from app.core.session_pool import get_session_pool

    checkout = bind_worker(repo)
    if dirty:
        Path(checkout.path, "keep-owner-work.txt").write_text("retain this checkout")
    # A real Git ref lock fails only branch deletion, AFTER worktree removal.
    lock = Path(repo[2], ".git", "refs", "heads", checkout.branch + ".lock")
    lock.write_text("held by this test")
    code = """import json, os, sys
for line in sys.stdin:
    try:
        result = {"cwd": os.getcwd()}
    except OSError as exc:
        result = {"error": type(exc).__name__, "errno": exc.errno}
    print(json.dumps(result), flush=True)
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=checkout.path,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def cwd():
        process.stdin.write("cwd\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())
    try:
        assert cwd() == {"cwd": checkout.path}
        retired_cwd = []
        async def retire(session_id, *, preserve_session):
            assert session_id == "followup" and preserve_session is True
            assert turn_scheduler.message_resume_lock(session_id).locked()
            retired_cwd.append(cwd())
            process.terminate()
            process.wait(timeout=3)
        removed = AsyncMock(side_effect=retire)
        monkeypatch.setattr(get_session_pool(), "remove", removed)
        reclaimed = await worker_checkout.reclaim_finished_worker_checkout(
            "followup", checkout if supplied_checkout else None)
        current = cwd() if process.poll() is None else retired_cwd[-1]
        trace = {"dirty": dirty, "supplied_checkout": supplied_checkout,
                 "reclaimed": reclaimed, "directory_exists": Path(checkout.path).is_dir(),
                 "branch_sha": git(repo[2], "rev-parse", "refs/heads/" + checkout.branch),
                 "pool_removals": removed.await_count, "old_process": current}
        print("PARTIAL_RECLAIM_TRACE " + json.dumps(trace))
        assert reclaimed is False, "branch cleanup failed or dirty checkout was retained"
        assert trace["branch_sha"] == checkout.base_commit
        assert trace["directory_exists"] is dirty
        assert removed.await_count == int(not dirty), trace
        if dirty:
            assert current == {"cwd": checkout.path}
        else:
            assert retired_cwd == [{"error": "FileNotFoundError", "errno": 2}]
            assert process.poll() is not None, "the deleted-cwd process must be retired"
            # The still-held ref lock also blocks Git checkout; admission fails closed.
            assert not await query_options.ensure_pending_checkout(database.get_session("followup"))
            assert not Path(checkout.path).exists()
            lock.unlink()
            # Retry the actual execution preflight after the other Git writer releases its lock.
            assert await query_options.ensure_pending_checkout(database.get_session("followup"))
            assert git(checkout.path, "branch", "--show-current") == checkout.branch
            assert git(checkout.path, "rev-parse", "HEAD") == checkout.base_commit
            fresh = subprocess.check_output([sys.executable, "-c", "import os; print(os.getcwd())"],
                                            cwd=checkout.path, text=True).strip()
            assert fresh == checkout.path
            print("RESTORED_PROCESS_CWD " + fresh)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
        lock.unlink(missing_ok=True)


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
