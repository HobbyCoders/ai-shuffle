"""In-process HTTP burst harness for #482; no agents, credentials or live app.

Run with pytest -s to print timing spans. Slow external work is simulated; DB
reads, routing, JSON serialization and (in the checkout test) Git are real.
"""
import asyncio
import threading
import time
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import git_operations as gitops
from app.api import network, swarms
from app.core import remote_task, swarm, turn_scheduler
from app.db import database, session_events
from app.network import catalog, transport

# Hang guard for waits that synchronize on events. It should never be reached
# when the code is correct, however slow a shared CI runner is (#794).
SAFETY_S = 30


async def burst(app, method, paths, bodies=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def one(index, path):
            started = time.perf_counter()
            response = await client.request(method, path, **({"json": bodies[index]} if bodies else {}))
            assert response.status_code == 200, response.text
            return response.json(), time.perf_counter() - started
        return await asyncio.gather(*(one(i, path) for i, path in enumerate(paths)))


def timings(name, results, spans):
    elapsed = sorted(row[1] for row in results)
    print(f"\n{name}: n={len(results)} p50={elapsed[len(elapsed)//2]:.3f}s max={elapsed[-1]:.3f}s spans={dict(spans)}")


@pytest.mark.asyncio
async def test_task_start_burst(monkeypatch):
    app = FastAPI()
    app.include_router(network.router)
    app.dependency_overrides[network.tokens.require_worker_device] = lambda: "peer"
    monkeypatch.setattr(transport, "note_contact", lambda *a: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda *a: None)
    # Cheap spawning, with the real handler's idempotency lock and publication.
    # A whole-catalog publication competes for the network sequence lock. Model
    # a modest 50ms pass, not the seconds observed on a loaded Windows worker.
    spans = Counter()
    sequence_lock = threading.Lock()
    def prepare(body, peer):
        time.sleep(.02)
        spans["prepared"] += 1
        return {"created": True, "worker_session_id": body["worker_session_id"]}
    def publish():
        with sequence_lock:
            time.sleep(.05)
            spans["whole_catalog_passes"] += 1
    def request_scan(*args, **kwargs):
        spans["scan_wakes"] += 1
        assert kwargs["session_ids"]
    monkeypatch.setattr(remote_task, "prepare_task_start", prepare)
    monkeypatch.setattr(catalog, "publish_owned", publish)
    monkeypatch.setattr(transport, "request_scan", request_scan)
    results = await burst(app, "POST", ["/api/v1/network/peer/control/task-start"] * 31,
                          [{"worker_session_id": f"child-{i}"} for i in range(31)])
    timings("task-start", results, spans)
    assert spans["prepared"] == 31
    assert spans["whole_catalog_passes"] == 0
    assert spans["scan_wakes"] == 31
    assert max(row[1] for row in results) < 3


@pytest.mark.asyncio
async def test_pr_state_burst(monkeypatch):
    app = FastAPI()
    app.include_router(gitops.router)
    app.dependency_overrides[gitops.require_auth] = lambda: "test"
    monkeypatch.setattr(gitops, "check_project_access", lambda *a: None)
    monkeypatch.setattr(gitops, "get_working_dir_async", AsyncMock(return_value="/fixture"))
    spans = Counter()
    def status(path):
        spans["full_status"] += 1
        time.sleep(.15)  # index/diff/ahead-behind work irrelevant to PR state
        return {"is_git_repo": True, "current_branch": "branch-149"}
    monkeypatch.setattr(gitops.git_service, "get_status", status)
    monkeypatch.setattr(gitops.git_service, "get_current_branch", lambda path: "branch-149")
    monkeypatch.setattr(gitops.git_service, "is_git_repo", lambda path: True)
    monkeypatch.setattr(gitops.github_service, "get_repo_from_remote", lambda path: "o/r")
    monkeypatch.setattr(gitops.github_service, "is_authenticated", lambda: True)
    pulls = [SimpleNamespace(number=i, head_branch=f"branch-{i}", state="open", title="PR", url=f"https://example.test/{i}", draft=False, mergeable=None) for i in range(150)]
    def list_pulls(repo, state="open", head=None, limit=100):
        spans["gh_calls"] += 1
        rows = [pr for pr in pulls if state == "open" and (head is None or pr.head_branch == head)][:limit]
        time.sleep(.02 + .002 * len(rows))
        return rows
    monkeypatch.setattr(gitops.github_service, "list_pulls", list_pulls)
    gitops._pr_state_cache.clear()
    try:
        results = await burst(app, "GET", ["/api/v1/projects/p/git/pr-state"] * 31)
        timings("pr-state", results, spans)
        assert all(row[0]["open_pr"] and row[0]["open_pr"]["number"] == 149 for row in results)
        assert spans["full_status"] == 0
        assert spans["gh_calls"] == 1
        assert max(row[1] for row in results) < 3
    finally:
        gitops._pr_state_cache.clear()


@pytest.mark.asyncio
async def test_swarm_list_burst(monkeypatch):
    database.create_profile("load", "Load", None, {})
    database.create_session("load", "load")
    # Drive the writer, rather than seeding a consumer-only projection. A long
    # tool transcript interleaves 120 full snapshots, each with 30 task cards.
    monkeypatch.setattr(__import__("app.core.history_backfill", fromlist=["ensure_backfilled"]),
                        "ensure_backfilled", AsyncMock())
    tasks = [{"id": str(i), "status": "running", "prompt": "brief" * 20} for i in range(30)]
    for step in range(120):
        session_events.append_events("load", [{"type": "message.delta", "payload": {"text": "x" * 1024}} for _ in range(100)])
        await swarm.persist({"id": "swarm_load", "session_id": "load", "status": "running", "tasks": tasks, "step": step})
    app = FastAPI()
    app.include_router(swarms.router, prefix="/api/sessions")
    app.dependency_overrides[swarms.require_auth] = lambda: "test"
    monkeypatch.setattr(swarms, "owned_session", AsyncMock(return_value={"id": "load"}))
    monkeypatch.setattr(swarms, "check_transcript_access", AsyncMock())
    spans = Counter()
    original_get_db = session_events.get_db
    @contextmanager
    def counted_db():
        spans["db_reads"] += 1
        with original_get_db() as conn:
            yield conn
    monkeypatch.setattr(session_events, "get_db", counted_db)
    results = await burst(app, "GET", ["/api/sessions/load/swarms"] * 31)
    timings("swarms", results, spans)
    assert all(row[0]["swarms"][0]["step"] == 119 for row in results)
    assert spans["db_reads"] == 31
    assert max(row[1] for row in results) < 3


@pytest.mark.asyncio
async def test_start_waiters_do_not_starve_short_reads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
    entered = asyncio.Event()
    release = threading.Event()
    active = 0
    guard = threading.Lock()
    # Saturate every admission thread before reading, so no read can borrow one.
    admission = remote_task._start_executor._max_workers
    def prepare(body, peer):
        nonlocal active
        with guard:
            active += 1
            if active == admission:
                loop.call_soon_threadsafe(entered.set)
        # Held until the reads below have finished, never released by a clock:
        # a read that needs one of these threads deadlocks and fails the test.
        # The bound only frees the threads if the test itself breaks.
        assert release.wait(SAFETY_S * 2)
        return {"created": True, "worker_session_id": body["worker_session_id"]}
    monkeypatch.setattr(remote_task, "prepare_task_start", prepare)
    monkeypatch.setattr(turn_scheduler, "kick", lambda *a: None)
    monkeypatch.setattr(transport, "request_scan", lambda *a, **kw: None)
    starts = [asyncio.create_task(remote_task.handle_task_start({"worker_session_id": f"blocked-{i}"}, "peer")) for i in range(31)]
    try:
        await asyncio.wait_for(entered.wait(), SAFETY_S)
        app = FastAPI()
        app.include_router(gitops.router)
        app.include_router(swarms.router, prefix="/api/sessions")
        app.dependency_overrides[gitops.require_auth] = lambda: "test"
        monkeypatch.setattr(gitops, "check_project_access", lambda *a: None)
        monkeypatch.setattr(gitops, "get_working_dir_async", AsyncMock(return_value="/blocked-load"))
        monkeypatch.setattr(gitops.git_service, "get_current_branch", lambda *a: "main")
        monkeypatch.setattr(gitops.git_service, "is_git_repo", lambda *a: True)
        monkeypatch.setattr(gitops.github_service, "get_repo_from_remote", lambda *a: None)
        monkeypatch.setattr(swarms, "owned_session", AsyncMock(return_value={"id": "load"}))
        monkeypatch.setattr(swarms, "check_transcript_access", AsyncMock())
        # Exercise both actual read handlers while all admission threads are
        # blocked. The default pool is deliberately small, as on small workers.
        # Starvation is proven by order, not speed: the reads must finish while
        # every admission thread is still blocked and no start has returned.
        # SAFETY_S is a hang guard for a starved read, not a latency budget.
        reads = await asyncio.wait_for(burst(app, "GET", [
            "/api/v1/projects/p/git/pr-state", "/api/sessions/load/swarms",
        ]), SAFETY_S)
        assert not release.is_set()
        assert active == admission
        assert not any(start.done() for start in starts)
        timings("reads-during-31-starts", reads, {"blocked_starts": len(starts)})
    finally:
        release.set()
        await asyncio.gather(*starts)
