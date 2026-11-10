"""#944: a follow-up to a worker whose switched checkout was cleaned up.

A Task worker switches its own checkout to a new branch, pushes it and opens a
PR; turn end moves the session binding to that branch, and finisher cleanup
then removes the pushed, clean checkout. A later follow-up must restore the
checkout at the same path on the bound branch, or be refused before any turn
is queued, never admitted into the missing folder. Real Git throughout.
"""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import turn_finalize, turn_scheduler, worker_checkout
from app.core.git_service import git_service
from app.db import checkouts, database
from tests.core.test_backend_followup_regressions import bind_worker
from tests.core.test_worker_checkout import repo, git  # noqa: F401
from tests.shared_fixtures import foreign_keys_off, pin_runner, clean_task_registry  # noqa: F401

SID = "followup"
FIX = "fix/followup-work"


@pytest.fixture
def cleaned(repo, tmp_path, monkeypatch):
    """The field sequence up to the follow-up: switch, commit, push, resync,
    finisher reclaim. Returns (checkout, pushed tip, base checkout)."""
    _, _, base, _ = repo
    checkout = bind_worker(repo, SID)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(base, "remote", "add", "origin", str(origin))
    git(base, "push", "origin", "main")
    git(checkout.path, "switch", "-c", FIX)
    (Path(checkout.path) / "work.txt").write_text("worker fix\n")
    git(checkout.path, "add", "work.txt")
    git(checkout.path, "commit", "-m", "worker fix")
    git(checkout.path, "push", "origin", FIX)
    tip = git(checkout.path, "rev-parse", "HEAD")
    # Turn end follows the worker's own switch.
    assert turn_finalize.resync_session_branch_from_head(SID) == (checkout.branch, FIX)
    assert database.get_session(SID)["branch"] == FIX
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    return checkout, tip, base


async def reclaim(checkout):
    # Intended cleanup: no work is lost, the work is on the pushed branch. The
    # first round's finisher passes the checkout it provisioned (provider_agent).
    assert await worker_checkout.reclaim_finished_worker_checkout(SID, checkout)
    assert not Path(checkout.path).exists()
    assert database.get_session(SID)["worktree_path"] == checkout.path


def branch_exists(base, name):
    return git_service._run_git(str(base), ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                                timeout=5).returncode == 0


async def refused(checkout, monkeypatch, match):
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match=match) as error:
        await turn_scheduler.on_agent_message(SID, "parent", "continue")
    enqueue.assert_not_awaited()
    note = str(error.value)
    assert f"'{FIX}'" in note and "Dispatch a new Task" in note
    assert str(Path(checkout.path).parent) not in note, "refusal must not name filesystem paths"
    return note


@pytest.mark.asyncio
async def test_followup_restores_switched_checkout_at_saved_path_on_bound_branch(cleaned, monkeypatch):
    checkout, tip, _ = cleaned
    await reclaim(checkout)

    async def enqueue(*args, **kwargs):
        assert Path(checkout.path).is_dir(), "follow-up admitted with the saved cwd missing"
        assert git(checkout.path, "branch", "--show-current") == FIX
        assert git(checkout.path, "rev-parse", "HEAD") == tip
        return {}

    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    await turn_scheduler.on_agent_message(SID, "parent", "continue")
    row = checkouts.get_checkout_by_path(checkout.path, project_id=checkout.project_v2_id)
    assert row["status"] == "active" and row["branch"] == FIX
    assert database.get_session(SID)["worktree_path"] == checkout.path


@pytest.mark.asyncio
async def test_branch_only_on_origin_is_restored_at_its_pushed_tip(cleaned, monkeypatch):
    checkout, tip, base = cleaned
    await reclaim(checkout)
    git(base, "branch", "-D", FIX)
    # Also the empty folder a Windows cwd lock can leave after cleanup.
    Path(checkout.path).mkdir()
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    await turn_scheduler.on_agent_message(SID, "parent", "continue")
    enqueue.assert_awaited_once()
    assert git(checkout.path, "branch", "--show-current") == FIX
    assert git(checkout.path, "rev-parse", "HEAD") == tip


@pytest.mark.asyncio
async def test_deleted_branch_is_refused_and_never_resurrected_at_starting_commit(cleaned, monkeypatch):
    checkout, _, base = cleaned
    await reclaim(checkout)
    git(base, "branch", "-D", FIX)
    git(base, "push", "origin", "--delete", FIX)
    await refused(checkout, monkeypatch, "no longer exists")
    assert not Path(checkout.path).exists()
    # The archived row records the starting commit; it is not this branch's work.
    assert not branch_exists(base, FIX)


@pytest.mark.asyncio
async def test_branch_checked_out_elsewhere_is_refused(cleaned, monkeypatch, tmp_path):
    checkout, _, base = cleaned
    await reclaim(checkout)
    other = tmp_path / "other"
    git(base, "worktree", "add", str(other), FIX)
    await refused(checkout, monkeypatch, "another folder")
    assert not Path(checkout.path).exists()
    assert git(other, "branch", "--show-current") == FIX


@pytest.mark.asyncio
async def test_interrupted_restore_is_not_accepted_on_next_message(cleaned, monkeypatch):
    checkout, _, base = cleaned
    await reclaim(checkout)
    assert worker_checkout.restore_worker_checkout(SID) == (True, None)
    # A process killed mid-add leaves the write-ahead marker and a partial tree.
    marker = git_service._worktree_materializing_marker(str(base), checkout.path)
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("incomplete: materializing\n")
    (Path(checkout.path) / "code.txt").unlink()
    await refused(checkout, monkeypatch, "part-way")


@pytest.mark.asyncio
async def test_execution_preflight_restores_switched_checkout(cleaned):
    from app.core import query_options

    checkout, tip, _ = cleaned
    await reclaim(checkout)
    # MessageSession and user-typed turns restore at execution instead.
    assert await query_options.ensure_pending_checkout(database.get_session(SID))
    assert git(checkout.path, "branch", "--show-current") == FIX
    assert git(checkout.path, "rev-parse", "HEAD") == tip


@pytest.mark.asyncio
async def test_switched_worker_with_existing_folder_is_unchanged(cleaned, monkeypatch):
    checkout, tip, _ = cleaned
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    await turn_scheduler.on_agent_message(SID, "parent", "continue")
    enqueue.assert_awaited_once()
    assert git(checkout.path, "rev-parse", "HEAD") == tip
    assert git(checkout.path, "branch", "--show-current") == FIX


@pytest.mark.asyncio
async def test_non_worker_session_with_missing_folder_is_unchanged(repo, monkeypatch):
    row, _, base, _ = repo
    missing = base / ".fluid" / "worktrees" / "feat-somebody"
    database.create_session("plain", "coder", project_v2_id=row["project_v2_id"],
                            worktree_path=str(missing), worktree_branch="feat/somebody")
    database.set_session_branch("plain", "feat/somebody", row["project_v2_id"])
    assert await worker_checkout.restore_worker_checkout_async("plain") == (True, None)
    assert worker_checkout.restore_worker_checkout("plain") == (True, None)
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    await turn_scheduler.on_agent_message("plain", "parent", "continue")
    enqueue.assert_awaited_once()
    assert not missing.exists()


# ── The code a queued turn actually runs: preflight, then the real option build ──

def build_options(cwd=None):
    from unittest.mock import patch

    from app.core import query_options as qo
    from tests.core.helpers import query_profile

    with patch.object(qo, "get_security_instructions", return_value=""):
        return qo.build_options_from_profile(profile=query_profile(), session_id=SID,
                                             overrides={"cwd": cwd} if cwd else None)


async def execute(cwd=None):
    from app.core import query_options as qo

    await qo.ensure_pending_checkout(database.get_session(SID))
    return build_options(cwd)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_cwd_override", [False, True])
async def test_execution_restores_and_resolves_options_on_bound_branch(cleaned, saved_cwd_override):
    checkout, tip, _ = cleaned
    await reclaim(checkout)
    # A worker turn is submitted with its saved cwd as an override (provider_agent).
    options, _ = await execute(checkout.path if saved_cwd_override else None)
    assert Path(str(options.cwd)).resolve() == Path(checkout.path).resolve()
    assert git(checkout.path, "branch", "--show-current") == FIX
    assert git(checkout.path, "rev-parse", "HEAD") == tip


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_cwd_override", [False, True])
async def test_execution_refusal_is_terminal_and_never_recreates_at_starting_commit(cleaned, saved_cwd_override):
    from app.core.query_options import WorktreeUnavailableError

    checkout, _, base = cleaned
    await reclaim(checkout)
    git(base, "branch", "-D", FIX)
    git(base, "push", "origin", "--delete", FIX)
    with pytest.raises(WorktreeUnavailableError) as error:
        await execute(checkout.path if saved_cwd_override else None)
    note = str(error.value)
    assert f"'{FIX}'" in note and "Dispatch a new Task" in note
    assert str(Path(checkout.path).parent) not in note, "refusal must not name filesystem paths"
    # Neither the recorded-tip recovery nor any other resolver ran.
    assert not branch_exists(base, FIX)
    assert not Path(checkout.path).exists()
    assert not any(Path(base, ".fluid", "worktrees").glob("*followup-work*"))


@pytest.mark.asyncio
async def test_origin_only_restore_refreshes_a_stale_tracking_ref(cleaned, monkeypatch, tmp_path):
    checkout, old_tip, base = cleaned
    await reclaim(checkout)
    git(base, "branch", "-D", FIX)
    # refs/remotes/origin/<branch> exists from the worker's push; origin moves on.
    origin = git(base, "remote", "get-url", "origin")
    clone = tmp_path / "other-clone"
    git(tmp_path, "clone", "-b", FIX, origin, str(clone))
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    (clone / "later.txt").write_text("pushed from elsewhere\n")
    git(clone, "add", "later.txt")
    git(clone, "commit", "-m", "later")
    git(clone, "push", "origin", FIX)
    new_tip = git(clone, "rev-parse", "HEAD")
    assert new_tip != old_tip
    options, _ = await execute()
    assert git(checkout.path, "rev-parse", "HEAD") == new_tip
    assert Path(str(options.cwd)).resolve() == Path(checkout.path).resolve()


@pytest.mark.asyncio
async def test_unreachable_origin_refuses_instead_of_trusting_cached_ref(cleaned, monkeypatch, tmp_path):
    checkout, _, base = cleaned
    await reclaim(checkout)
    git(base, "branch", "-D", FIX)
    git(base, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    note = await refused(checkout, monkeypatch, "could not be reached")
    assert "origin" in note
    assert not Path(checkout.path).exists()
    assert not branch_exists(base, FIX)


@pytest.mark.asyncio
async def test_preflight_waits_for_a_finisher_removing_an_existing_switched_checkout(cleaned):
    import asyncio

    from app.core import query_options as qo

    checkout, tip, _ = cleaned
    row = database.get_session(SID)
    assert Path(checkout.path).is_dir()
    async with turn_scheduler.message_resume_lock(SID):
        # The folder exists when the turn starts; a finisher holding the lock
        # removes it before the preflight may look.
        preflight = asyncio.create_task(qo.ensure_pending_checkout(row))
        await asyncio.sleep(0)
        assert await asyncio.to_thread(worker_checkout.reclaim_untouched_worker_checkout, SID, checkout)
        assert not Path(checkout.path).exists()
        assert not preflight.done(), "preflight ran without waiting for the finisher's lock"
    await preflight
    options, _ = build_options()
    assert git(checkout.path, "branch", "--show-current") == FIX
    assert git(checkout.path, "rev-parse", "HEAD") == tip
    assert Path(str(options.cwd)).resolve() == Path(checkout.path).resolve()


@pytest.mark.asyncio
async def test_unowned_sessions_do_not_take_the_message_lock(repo, monkeypatch):
    from app.core import query_options as qo

    row, _, base, _ = repo
    taken = []
    real = turn_scheduler.message_resume_lock
    monkeypatch.setattr(turn_scheduler, "message_resume_lock", lambda sid: taken.append(sid) or real(sid))
    other = bind_worker(repo, "someone-else")
    cases = {
        # A plain branch session, its folder missing and present.
        "plain-missing": str(base / ".fluid" / "worktrees" / "feat-somebody"),
        "plain-present": str(base),
        # A worker whose saved cwd is another worker's checkout, not its own.
        "task-unowned": other.path,
    }
    for sid, path in cases.items():
        database.create_session(sid, "coder", project_v2_id=row["project_v2_id"],
                                worktree_path=path, worktree_branch="feat/somebody")
        database.set_session_branch(sid, "feat/somebody", row["project_v2_id"])
        await qo.ensure_pending_checkout(database.get_session(sid))
    assert taken == []
