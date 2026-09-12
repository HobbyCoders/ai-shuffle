"""PR refresh recovery uses the real session writer and broadcast path."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_pr_state as prs
from app.core.github_service import GitHubCliError
from app.db import database


@pytest.fixture(autouse=True)
def isolated_scheduler(monkeypatch):
    monkeypatch.setattr(prs, "_reconcile_tasks", {})
    monkeypatch.setattr(prs, "_reconcile_again", set())
    monkeypatch.setattr(prs, "_reconcile_slots", asyncio.Semaphore(4))
    prs._reconcile_recent.clear()
    yield
    prs._reconcile_recent.clear()


@pytest.mark.asyncio
async def test_settled_open_pr_recovers_after_failed_lookup(mock_db):
    database.create_project("p", "Project", None, "/tmp/p")
    database.create_session(session_id="s", profile_id="default", project_id="p",
                            worktree_branch="feat/old")
    database.set_session_branch("s", "feat/current")
    database.set_session_active("s", False)
    merged = SimpleNamespace(number=1226, state="MERGED", head_branch="feat/current",
                             url="https://github.com/o/r/pull/1226")
    with patch("app.core.sync_engine.sync_engine") as sync, patch(
        "app.api.git_helpers.get_working_dir_async", AsyncMock(return_value="/wd")
    ), patch("app.core.github_service.github_service") as gh, patch(
        "app.core.session_pr_state.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        # Seed through the same writer used when an agent opens a PR.
        assert await prs.persist_pr_state(session_id="s", pr_number=1226,
                                          pr_url=merged.url, pr_state="open")
        sync.broadcast_user_session_updated.reset_mock()
        gh.get_repo_from_remote.return_value = "o/r"
        gh.list_pulls.side_effect = [GitHubCliError("offline"), [merged]]
        await prs.schedule_session_pr_reconcile("s")
        assert gh.list_pulls.call_count == 2
        assert gh.list_pulls.call_args.kwargs["head"] == "feat/current"
        sleep.assert_awaited_once_with(2)
        patch_data = sync.broadcast_user_session_updated.await_args.kwargs
        assert patch_data["updates"]["pr_state"] == "merged"
    row = database.get_session("s")
    assert row["pr_state"] == "merged"
    assert row["pr_number"] == 1226
    assert not row["is_active"]
    assert not prs._reconcile_tasks


@pytest.mark.asyncio
async def test_refresh_retries_write_failure(mock_db):
    session = {"id": "s", "project_id": "p", "branch": "feat/x", "pr_state": "open"}
    pr = SimpleNamespace(number=1, state="MERGED", head_branch="feat/x", url="url")
    with patch("app.db.database.get_session", return_value=session), patch(
        "app.api.git_helpers.get_working_dir_async", AsyncMock(return_value="/wd")
    ), patch("app.core.github_service.github_service") as gh, patch.object(
        prs, "persist_pr_state", AsyncMock(side_effect=[False, True])
    ) as persist, patch("app.core.session_pr_state.asyncio.sleep", AsyncMock()):
        gh.get_repo_from_remote.return_value = "o/r"
        gh.list_pulls.return_value = [pr]
        await prs.schedule_session_pr_reconcile("s", force=True)
        assert persist.await_count == 2


@pytest.mark.asyncio
async def test_poll_coalescing_cooldown_and_forced_trailing_refresh(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def reconcile(sid):
        calls.append(sid)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(prs, "reconcile_session_pr", reconcile)
    task = prs.schedule_session_pr_reconcile("s")
    await started.wait()
    assert prs.schedule_session_pr_reconcile("s") is task
    assert prs.schedule_session_pr_reconcile("s", force=True) is task
    release.set()
    await task
    assert calls == ["s", "s"]  # pre-merge answer cannot consume turn-end refresh
    assert prs.schedule_session_pr_reconcile("s") is None
    await prs.schedule_session_pr_reconcile("s", force=True)
    assert calls == ["s", "s", "s"]


@pytest.mark.asyncio
async def test_failures_are_bounded_and_next_poll_can_recover(caplog):
    with patch.object(prs, "reconcile_session_pr", AsyncMock(return_value=False)) as rec, patch(
        "app.core.session_pr_state.asyncio.sleep", AsyncMock()
    ):
        await prs.schedule_session_pr_reconcile("s")
        assert rec.await_count == 3
        assert "exhausted retries" in caplog.text
        assert prs.schedule_session_pr_reconcile("s") is None
        prs._reconcile_recent.clear()  # next poll after cooldown
        rec.return_value = True
        await prs.schedule_session_pr_reconcile("s")
        assert rec.await_count == 4
    assert not prs._reconcile_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("before_start", [False, True])
async def test_cancelled_refresh_releases_session_slot(before_start):
    started = asyncio.Event()

    async def blocked(sid):
        started.set()
        await asyncio.Event().wait()

    with patch.object(prs, "reconcile_session_pr", blocked):
        task = prs.schedule_session_pr_reconcile("s")
        if not before_start:
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not prs._reconcile_tasks
    assert not prs._reconcile_again
