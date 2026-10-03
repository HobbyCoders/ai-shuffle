"""Worktree janitor report: decide reclaimable without ever deleting."""
import subprocess

from app.core import worktree_report as wr

PORCELAIN = """worktree /repo
HEAD aaa
branch refs/heads/main

worktree /repo/.fluid/worktrees/fix-a
HEAD bbb
branch refs/heads/fix/a

worktree /repo/.fluid/worktrees/locked
HEAD ccc
branch refs/heads/feat/b
locked

worktree /repo/.fluid/worktrees/gone
HEAD ddd
detached
prunable gitdir file points to non-existent location
"""


def test_parse_worktree_list():
    items = wr.parse_worktree_list(PORCELAIN)
    assert [i["path"] for i in items] == ["/repo", "/repo/.fluid/worktrees/fix-a",
                                          "/repo/.fluid/worktrees/locked", "/repo/.fluid/worktrees/gone"]
    assert items[1]["branch"] == "fix/a" and items[1]["head"] == "bbb"
    assert items[2]["locked"] is True
    assert items[3]["prunable"] is True and items[3]["branch"] is None


def _item(**extra):
    return {"path": "/wt", "branch": "fix/a", "head": "bbb", "locked": False, "prunable": False, **extra}


def test_merged_clean_and_idle_is_reclaimable():
    row = wr.assess(_item(), [{"id": "s1", "pr_state": "merged", "running": False}], 0, 0, False)
    assert row["reclaimable"] is True and row["reason"] == "safe to remove"


def test_every_blocker_is_named():
    row = wr.assess(_item(locked=True), [{"id": "s1", "pr_state": "open", "running": True}], 3, 2, False)
    assert row["reclaimable"] is False
    for reason in ("a session is working in it", "locked", "uncommitted changes", "PR still open",
                   "2 unpushed commit(s)"):
        assert reason in row["reason"]


def test_branch_merged_without_pr_is_reclaimable_and_unknown_status_is_not():
    assert wr.assess(_item(), [], 0, 0, True)["reclaimable"] is True
    assert wr.assess(_item(), [], None, 0, True)["reason"] == "status unknown"
    assert "no closed PR" in wr.assess(_item(), [], 0, 0, False)["reason"]


def test_parses_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args, cwd=repo: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    run("worktree", "add", "-q", "-b", "fix/x", str(tmp_path / "wt"))
    items = wr.parse_worktree_list(wr._git(str(repo), "worktree", "list", "--porcelain"))
    assert [i["branch"] for i in items] == ["main", "fix/x"]
