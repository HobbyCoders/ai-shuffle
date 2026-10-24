"""Concurrent receives retain branch/SHA isolation without serial network I/O."""
import asyncio
import threading
import time
from collections import Counter
from uuid import uuid4

import pytest

from app.core import checkout_transfer, worker_checkout
from tests.core.test_remote_task import (  # noqa: F401 — pytest fixtures
    PARENT,
    body_for,
    child_turns,
    git,
    kicks,
    net,
    no_credential_prompts,
    world,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["branch", "default"])
async def test_two_receives_overlap_fetches_and_keep_pinned_checkouts(world, monkeypatch, mode):  # noqa: F811
    ids = ["receive-load-a", "receive-load-b"]
    remote = checkout_transfer.origin_identity(str(world.target))
    if mode == "branch":
        refs = [f"HEAD:refs/heads/{worker_checkout.worker_branch(key)}" for key in ids]
        git(world.parent, "push", "origin", *refs)
        states = [worker_checkout.RemoteWorkerBranch(
            mode="branch", project_v2_id=world.project["id"],
            branch=worker_checkout.worker_branch(key), base_commit=world.tip,
            remote=remote, pushed=True, parent_branch="main",
        ) for key in ids]
    else:
        states = [worker_checkout.RemoteWorkerBranch(mode="default", project_v2_id=world.project["id"])
                  for _ in ids]
    active, peak = 0, 0
    guard = threading.Lock()
    spans = Counter()
    original_run = checkout_transfer._run
    from app.core.git_service import git_service
    original_service_run = git_service._run_git
    def timed(fn, args, kwargs):
        nonlocal active, peak
        if not any(arg == "fetch" or isinstance(arg, list) and "fetch" in arg for arg in args):
            return fn(*args, **kwargs)
        with guard:
            active += 1
            peak = max(peak, active)
        started = time.perf_counter()
        try:
            time.sleep(.15)  # network latency; actual fetch follows
            return fn(*args, **kwargs)
        finally:
            with guard:
                active -= 1
                spans["fetch_seconds"] += time.perf_counter() - started
                spans["fetches"] += 1
    monkeypatch.setattr(checkout_transfer, "_run", lambda *a, **kw: timed(original_run, a, kw))
    monkeypatch.setattr(git_service, "_run_git", lambda *a, **kw: timed(original_service_run, a, kw))
    started = time.perf_counter()
    with world.net.as_target():
        rows = await asyncio.gather(*(asyncio.to_thread(worker_checkout.receive_worker_branch, state, key)
                                      for state, key in zip(states, ids)))
    elapsed = time.perf_counter() - started
    print(f"\nreceive-{mode}: n=2 elapsed={elapsed:.3f}s peak_fetches={peak} spans={dict(spans)}")
    assert peak == 2
    assert len({row.cwd for row in rows}) == 2
    for row, key in zip(rows, ids):
        assert git(row.cwd, "branch", "--show-current") == worker_checkout.worker_branch(key)
        assert git(row.cwd, "rev-parse", "HEAD") == row.checkout.base_commit
    assert git(world.target, "branch", "--show-current") == "main"


@pytest.mark.asyncio
async def test_31_real_starts_commit_receipts_without_launching_agents(world, monkeypatch):  # noqa: F811
    from fastapi import FastAPI

    from app.api import network
    from app.db import database, remote_tasks
    from app.network import transport
    from tests.api.test_load_482 import burst, timings

    app = FastAPI()
    app.include_router(network.router)
    app.dependency_overrides[network.tokens.require_worker_device] = lambda: PARENT
    monkeypatch.setattr(transport, "note_contact", lambda *a: None)
    wakes = []
    monkeypatch.setattr(transport, "request_scan", lambda *a, **kw: wakes.extend(kw["session_ids"]))
    ids = [str(uuid4()) for _ in range(31)]
    bodies = [body_for(world, worker_id=key, state={"mode": "none"}) for key in ids]
    # Only execution is suppressed by world's scheduler kick fixture. Parsing,
    # authorization, session/turn creation, permissions, retry and report debt
    # all use the real producer. No provider/CLI process is launched.
    with world.net.as_target():
        results = await burst(app, "POST", ["/api/v1/network/peer/control/task-start"] * 31, bodies)
        retries = await burst(app, "POST", ["/api/v1/network/peer/control/task-start"] * 31, bodies)
    timings("task-start-real-producer", results, {"scheduler_kicks": len(world.kicks)})
    assert len(world.kicks) == len(wakes) == 31
    assert max(row[1] for row in results) < 5
    for key, (answer, _), (retry, _) in zip(ids, results, retries):
        assert answer["created"] and not retry["created"]
        assert answer["worker_session_id"] == key
        assert len(child_turns(key)) == 1
        assert database.get_session(key)["permission_mode"] == "bypassPermissions"
        assert remote_tasks.get_child(key) is not None
