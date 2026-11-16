"""Merge when green: verdicts, the watch loop, and the busy-branch guard."""
from unittest.mock import AsyncMock, patch

import pytest

from app.core import auto_merge as am


@pytest.fixture(autouse=True)
def _fast():
    am._watches.clear()
    with patch.object(am, "POLL_S", 0):
        yield
    am._watches.clear()


def test_checks_verdict():
    ok = {"status": "COMPLETED", "conclusion": "SUCCESS"}
    assert am.checks_verdict([]) == "pending"
    assert am.checks_verdict([ok, {"status": "COMPLETED", "conclusion": "SKIPPED"}]) == "passed"
    assert am.checks_verdict([ok, {"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert am.checks_verdict([ok, {"status": "COMPLETED", "conclusion": "FAILURE"}]) == "failed"
    assert am.checks_verdict([{"__typename": "StatusContext", "state": "SUCCESS"}]) == "passed"
    assert am.checks_verdict([{"__typename": "StatusContext", "state": "PENDING"}]) == "pending"


def _watch():
    return am.Watch(session_id="s1", project_id="p1", repo="o/r", pr_number=7, method="squash", working_dir="/wt")


GREEN = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefName": "fix/x", "url": "u",
         "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}


async def test_merges_when_green_and_idle():
    watch = _watch()
    am._watches["s1"] = watch
    views = iter([{**GREEN, "statusCheckRollup": [{"status": "IN_PROGRESS"}]}, GREEN])
    with patch.object(am, "_pr_view", side_effect=lambda *a: next(views)), \
            patch.object(am, "_idle", AsyncMock(return_value=True)), \
            patch.object(am, "_merge", AsyncMock(return_value="merged!")) as merge, \
            patch.object(am, "_record", AsyncMock()) as record:
        await am._run(watch)
    merge.assert_awaited_once()
    assert record.await_args.args[1] == "merged!"
    assert "s1" not in am._watches


async def test_waits_while_another_session_works_in_the_branch():
    watch = _watch()
    idle = AsyncMock(side_effect=[False, False, True])
    with patch.object(am, "_pr_view", return_value=GREEN), patch.object(am, "_idle", idle), \
            patch.object(am, "_merge", AsyncMock(return_value="merged")) as merge, \
            patch.object(am, "_record", AsyncMock()):
        await am._run(watch)
    assert idle.await_count == 3
    merge.assert_awaited_once()


@pytest.mark.parametrize("view,expected", [
    ({**GREEN, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}, "CI check failed"),
    ({**GREEN, "mergeable": "CONFLICTING"}, "merge conflicts"),
    ({**GREEN, "state": "CLOSED"}, "was closed"),
    ({**GREEN, "state": "MERGED"}, "already merged"),
])
async def test_stops_with_a_reason(view, expected):
    with patch.object(am, "_pr_view", return_value=view), \
            patch.object(am, "_merge", AsyncMock()) as merge, \
            patch.object(am, "_record", AsyncMock()) as record:
        await am._run(_watch())
    merge.assert_not_awaited()
    assert expected in record.await_args.args[1]


async def test_budget_ends_the_watch():
    with patch.object(am, "BUDGET_S", 0), patch.object(am, "_record", AsyncMock()) as record:
        await am._run(_watch())
    assert "without a green, idle PR #7" in record.await_args.args[1]


async def test_merge_never_uses_delete_branch_and_records_merged():
    from app.core.github_service import github_service

    deleted = type("R", (), {"returncode": 0})()
    with patch.object(github_service, "merge_pull", return_value=(True, None)) as merge, \
            patch.object(github_service, "_run_gh", return_value=deleted) as gh, \
            patch("app.core.session_pr_state.persist_pr_state", AsyncMock()) as persist:
        text = await am._merge(_watch(), GREEN)
    merge.assert_called_once_with("o/r", 7, "squash")
    assert gh.call_args.args[0] == ["api", "-X", "DELETE", "repos/o/r/git/refs/heads/fix/x"]
    assert persist.await_args.kwargs["pr_state"] == "merged"
    assert text == "Merge when green: CI passed, merged PR #7 and deleted its remote branch."


async def test_enable_requires_an_open_pr():
    with patch.object(am, "_session_target", side_effect=ValueError("This session has no open pull request to merge.")):
        with pytest.raises(ValueError, match="no open pull request"):
            await am.enable("s1")
    assert am.status("s1") == {"enabled": False, "pr_number": None}


def test_a_mirrored_chat_merges_in_this_workers_checkout_of_its_project(mock_db, tmp_path):
    """#1659: a chat another worker runs names only its shared project here.

    It must resolve this worker's checkout of that project, never an
    unrelated project that happens to share the owner's legacy id."""
    from app.db import database
    from app.db.base import get_db
    from app.db.projects_v2 import project_v2_id_for_origin

    database.create_project("sandbox", "Unrelated", None, str(tmp_path / "unrelated"),
                            link_group_id="git:acme/unrelated")
    database.create_project("checkout", "Shared", None, str(tmp_path / "shared"), link_group_id="git:acme/shared")
    database.create_session("s1", "default", title="t")
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET project_id = NULL, project_v2_id = %s, pr_number = 7, pr_state = 'open' WHERE id = 's1'",
            (project_v2_id_for_origin("git:acme/shared"),))
    with patch("app.core.github_service.github_service.get_repo_from_remote",
               side_effect=lambda path: "acme/shared" if path.endswith("shared") else "acme/unrelated"):
        target = am._session_target("s1")
    assert (target["project_id"], target["repo"]) == ("checkout", "acme/shared")
