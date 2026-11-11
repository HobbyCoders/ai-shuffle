"""#1481: a follow-up to a worker whose untouched checkout cleanup stopped part-way.

Windows will not delete a directory a live process uses as its cwd. ``git
worktree remove`` then deletes the files and unregisters the worktree but
leaves the empty folder, and the worker's branch is kept. A later follow-up on
the worker's own branch must restore the checkout at its saved path, as the
switched-branch path already does (#1440), not refuse it as "no longer on its
assigned branch". Real Git throughout; on Windows the leftover comes from a
real process holding the folder as its cwd.
"""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from app.core import query_options, turn_scheduler, worker_checkout
from app.db import database
from tests.core.test_backend_followup_regressions import bind_worker
from tests.core.test_worker_checkout import repo, git  # noqa: F401
from tests.shared_fixtures import foreign_keys_off, pin_runner, clean_task_registry  # noqa: F401

SID = "followup"


def leave_empty_leftover(repo, checkout):
    """Leave the state a cwd-locked removal leaves: an empty, unregistered
    folder at the saved path and the worker's branch still at its base."""
    base = repo[2]
    if os.name == "nt":
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                  cwd=checkout.path, stdin=subprocess.PIPE)
        try:
            # The reclaim helper alone, without the finisher's pool retirement.
            assert worker_checkout.reclaim_untouched_worker_checkout(SID, checkout) is False
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
    else:
        # POSIX deletes a live cwd, so reproduce Git's outcome directly.
        git(base, "worktree", "remove", checkout.path)
        Path(checkout.path).mkdir()
    assert Path(checkout.path).is_dir() and not any(Path(checkout.path).iterdir())
    registered = [line.removeprefix("worktree ") for line in git(base, "worktree", "list", "--porcelain").splitlines()
                  if line.startswith("worktree ")]
    assert not any(same(path, checkout.path) for path in registered), registered
    assert git(base, "rev-parse", "refs/heads/" + checkout.branch) == checkout.base_commit


def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def assert_restored(checkout):
    assert git(checkout.path, "branch", "--show-current") == checkout.branch
    assert git(checkout.path, "rev-parse", "HEAD") == checkout.base_commit
    assert same(git(checkout.path, "rev-parse", "--show-toplevel"), checkout.path)


@pytest.mark.asyncio
async def test_peer_followup_restores_own_checkout_over_empty_leftover(repo, monkeypatch):
    checkout = bind_worker(repo, SID)
    leave_empty_leftover(repo, checkout)

    async def enqueue(*args, **kwargs):
        assert_restored(checkout)
        return {}

    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    await turn_scheduler.on_agent_message(SID, "parent", "continue")
    assert database.get_session(SID)["worktree_path"] == checkout.path


@pytest.mark.asyncio
async def test_execution_preflight_restores_own_checkout_over_empty_leftover(repo):
    checkout = bind_worker(repo, SID)
    leave_empty_leftover(repo, checkout)
    assert await query_options.ensure_pending_checkout(database.get_session(SID))
    assert_restored(checkout)


@pytest.mark.asyncio
async def test_nonempty_leftover_is_still_refused(repo):
    checkout = bind_worker(repo, SID)
    leave_empty_leftover(repo, checkout)
    Path(checkout.path, "stray.txt").write_text("not ours to discard\n")
    ok, note = worker_checkout.restore_worker_checkout(SID)
    assert not ok and note
    assert Path(checkout.path, "stray.txt").read_text() == "not ours to discard\n"
