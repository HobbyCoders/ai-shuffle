"""A session's bound checkout cannot have its branch changed by another session.

Real Git and the real session/checkout tables; only provider inference is
absent. The headline case is the reported one: a chat on its own worktree
starts a peer with StartSession, and the peer runs ``git checkout -b`` in the
first chat's folder.
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from app.core import checkout_lock
from app.core import session_orchestration as orch
from app.core.config import settings
from app.db import checkouts, database, projects_v2, sessions, turns
from app.db.base import get_db
from tests.shared_fixtures import foreign_keys_off, pin_runner  # noqa: F401


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def branch_of(path):
    return git(path, "branch", "--show-current")


def chat(sid, project_id, branch=None):
    row = sessions.create_session(sid, "agent", title=f"chat {sid}", is_active=True,
                                  model="sonnet", permission_mode="default",
                                  project_v2_id=project_id)
    if branch:
        database.set_session_branch(sid, branch, project_id)
    turn = turns.enqueue_turn(sid, "user", prompt="work", profile_id="agent")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
    return row


@pytest.fixture
def project(mock_db, tmp_path, monkeypatch):
    """A base checkout plus the parent chat's own worktree on ``parent-branch``."""
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-b", "main")
    git(base, "config", "user.name", "Lock Test")
    git(base, "config", "user.email", "lock@example.test")
    (base / ".gitignore").write_text(".fluid/\n")
    (base / "code.txt").write_text("base\n")
    git(base, "add", ".gitignore", "code.txt")
    git(base, "commit", "-m", "base")
    parent = tmp_path / "parent"
    git(base, "worktree", "add", "-b", "parent-branch", str(parent))
    (parent / "code.txt").write_text("parent work\n")
    git(parent, "commit", "-am", "parent work")
    pv2 = projects_v2.upsert_project("local:checkout-lock-test", name="Lock repo")
    runner = settings.effective_instance_id
    checkouts.upsert_checkout(pv2["id"], runner, str(base))
    checkouts.upsert_checkout(pv2["id"], runner, str(parent), branch="parent-branch")
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    chat("parent", pv2["id"], branch="parent-branch")
    # The parent is the checkout's original owner: older than any chat below.
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET created_at=%s WHERE id=%s",
                              ("2026-01-01 00:00:00", "parent"))
    return pv2["id"], base, parent


def queue_turn(sid):
    """Give a chat queued work, as a chat in the middle of something has."""
    turns.enqueue_turn(sid, "user", prompt="more work", profile_id="agent")


def worktrees(base):
    return git(base, "worktree", "list").count("\n") + 1


def bash(command):
    return {"command": command}


def run_guarded(session_id, command, cwd):
    """Dispatch a real fluid Bash call through the guarded registry."""
    from fluid.tools.builtins import bash as bash_tool
    from fluid.tools.registry import ToolCtx, ToolRegistry

    registry = ToolRegistry()
    registry.register_builtin(bash_tool.SPEC, bash_tool.handler)
    checkout_lock.install_registry_guard(registry, session_id, str(cwd))
    return asyncio.run(registry.dispatch("Bash", bash(command), ToolCtx(cwd=str(cwd))))


def test_start_session_child_gets_its_own_worktree_and_cannot_switch_the_parents(project):
    pv2, base, parent = project
    tip = git(parent, "rev-parse", "HEAD")
    result = orch._queue("parent", {"title": "Stable channel", "prompt": "Fix the channel switch",
                                    "request_id": "lock-scenario"}, "start")
    child_id = result["session_id"]
    child = sessions.get_session(child_id)
    branch = f"chore/chat-{child_id[:8]}"

    # Prevent: its own app-provisioned worktree on a pushable branch, never the parent's.
    assert child["branch"] == child["worktree_branch"] == branch
    assert child["worktree_id"] is None and child["project_v2_id"] == pv2
    own = Path(child["worktree_path"])
    assert own.is_dir() and own.resolve() != parent.resolve()
    assert branch_of(own) == branch
    assert git(own, "rev-parse", "HEAD") == tip
    assert result["checkout"]["branch"] == branch
    turn = turns.get_turn(result["turn_id"])
    assert "cwd" not in (turn["overrides"] or {})
    note = turn["overrides"]["system_prompt_append"]
    assert str(own) in note and "(branch 'parent-branch')" in note

    # Lock: the child's `checkout -b` in the parent's folder is refused, even
    # while the parent is idle...
    for command, cwd in (("git checkout -b fix/stable-channel-switch-back", parent),
                         (f"cd {parent} && git checkout -b fix/stable-channel-switch-back", own),
                         (f"git -C {parent} switch main", own)):
        outcome = run_guarded(child_id, command, cwd)
        assert outcome.is_error
        assert "Refused" in outcome.text and "chat parent" in outcome.text
        assert "parent-branch" in outcome.text and f"own checkout ({own})" in outcome.text
    # ...nothing ran, and the parent is unaffected.
    assert branch_of(parent) == "parent-branch"
    assert not git(base, "branch", "--list", "fix/stable-channel-switch-back")
    parent_row = sessions.get_session("parent")
    assert parent_row["branch"] == "parent-branch"
    from app.core import project_resolver
    assert project_resolver.resolve_existing_cwd(dict(parent_row)) == str(parent)

    # The owners' own operations keep working.
    outcome = run_guarded(child_id, "git checkout -b fix/child-work", own)
    assert not outcome.is_error, outcome.text
    assert branch_of(own) == "fix/child-work"
    assert checkout_lock.tool_denial("parent", "Bash", bash("git checkout -b parent-next"), str(parent)) is None


def test_a_parent_with_a_legacy_co_bound_child_keeps_its_own_checkout(project):
    """A chat started before this fix is bound to the parent's branch too. The
    parent, the checkout's original owner, is never blocked by it."""
    pv2, _base, parent = project
    chat("legacy-child", pv2, branch="parent-branch")
    for command in ("git checkout -b fix/new", "git rebase main", "git switch parent-branch"):
        assert checkout_lock.tool_denial("parent", "Bash", bash(command), str(parent)) is None
    queue_turn("legacy-child")  # even while the child is busy
    outcome = run_guarded("parent", "git checkout -b fix/new", parent)
    assert not outcome.is_error, outcome.text
    assert branch_of(parent) == "fix/new"


def test_a_co_bound_chat_is_blocked_only_while_the_owner_works(project):
    pv2, _base, parent = project
    chat("legacy-child", pv2, branch="parent-branch")
    assert checkout_lock.tool_denial("legacy-child", "Bash", bash("git switch main"), str(parent)) is None
    queue_turn("parent")
    denial = checkout_lock.tool_denial("legacy-child", "Bash", bash("git switch main"), str(parent))
    assert denial and "bound to that checkout too" in denial


def test_an_unbound_chat_is_refused_while_the_owner_is_idle(project):
    pv2, _base, parent = project
    chat("stranger", pv2)
    denial = checkout_lock.tool_denial("stranger", "Bash", bash("git switch main"), str(parent))
    assert denial and "Refused" in denial


def test_archived_sessions_and_the_base_checkout_are_not_locked(project):
    pv2, base, parent = project
    chat("other", pv2)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET status='archived' WHERE id=%s", ("parent",))
    assert checkout_lock.tool_denial("other", "Bash", bash("git switch main"), str(parent)) is None
    # The project's base checkout records no branch and is shared by design.
    assert checkout_lock.tool_denial("other", "Bash", bash("git checkout -b x"), str(base)) is None


@pytest.mark.parametrize("command", [
    "git branch -m renamed",
    "gh pr checkout 12",
    "git checkout main 2>&1 | tail -3",
    "git checkout -",
    "bash -lc 'git switch -c sneaky'",
])
def test_other_branch_changes_in_a_foreign_checkout_are_refused(project, command):
    pv2, _base, parent = project
    chat("other", pv2)
    assert checkout_lock.tool_denial("other", "Bash", bash(command), str(parent))


def test_branch_ops_aimed_by_name_or_path_are_refused(project):
    pv2, base, parent = project
    chat("other", pv2)
    denial = checkout_lock.tool_denial("other", "Bash", bash(f"git worktree remove {parent}"), str(base))
    assert denial and "archives session parent" in denial
    assert checkout_lock.tool_denial("other", "Bash", bash(f"git worktree move {parent} {parent}-moved"),
                                     str(base))
    assert checkout_lock.tool_denial("other", "Bash", bash("git branch -D parent-branch"), str(base))
    assert checkout_lock.tool_denial("other", "Bash", bash("git branch -f parent-branch main"), str(base))


@pytest.mark.parametrize("command", [
    "git status && git log --oneline -3",
    "git checkout -- code.txt",
    "git checkout code.txt",
    "git checkout parent-branch",
    "git checkout $(git rev-parse HEAD)",
    "git reset HEAD code.txt",
    "git reset --hard main",
    "git rebase main",
    "git rebase --abort",
    "git rebase --continue",
    "cat > s.sh <<'EOF'\ngit checkout -b x\ngit switch main\nEOF\nbash -n s.sh",
    "echo 'git checkout -b x'",
    "git commit -m 'git switch main'",
    "git branch -a",
])
def test_non_switching_commands_in_a_foreign_checkout_run(project, command):
    pv2, _base, parent = project
    chat("other", pv2)
    assert checkout_lock.tool_denial("other", "Bash", bash(command), str(parent)) is None


def test_non_shell_tools_and_unbound_sessions_are_ignored(project):
    _pv2, _base, parent = project
    assert checkout_lock.tool_denial("other", "Read", {"file_path": "x"}, str(parent)) is None
    assert checkout_lock.tool_denial(None, "Bash", bash("git switch main"), str(parent)) is None
    assert checkout_lock.tool_denial("other", "mcp__builtins__Monitor",
                                     {"command": "git switch main", "cwd": str(parent)}, None)


def test_delegated_policy_refuses_with_the_cli_shell_directory(project):
    """The claude CLI's own Bash is checked by the PreToolUse policy, using the
    directory the CLI reports (its shell keeps a ``cd``)."""
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    pv2, base, parent = project
    chat("other", pv2)
    options = SessionOptions(model="opus", cwd=str(base),
                             extra={"app_session_id": "other", "profile_id": "agent"})
    policy = ha._delegated_tool_policy(options, None, [])
    assert policy("Bash", bash("git switch main")) is None  # its own (base) folder
    assert "Refused" in policy("Bash", bash("git switch main"), cwd=str(parent))
    assert policy("Bash", bash("git switch main"), cwd=".") is None  # relative: session cwd


def test_a_failed_start_reclaims_the_checkout_it_made(project, monkeypatch):
    _pv2, base, parent = project
    made = []
    real = orch._start_checkout

    def record(sender, new_id):
        made.append(real(sender, new_id))
        return made[-1]

    def fail(**kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(orch, "_start_checkout", record)
    monkeypatch.setattr(sessions, "create_session", fail)
    with pytest.raises(RuntimeError, match="insert failed"):
        orch._queue("parent", {"title": "t", "prompt": "p", "request_id": "fails"}, "start")
    assert made and not Path(made[0].path).exists()
    assert not git(base, "branch", "--list", made[0].branch)
    assert branch_of(parent) == "parent-branch"


def test_the_checkout_is_made_outside_the_chain_lock_and_dropped_for_a_duplicate(project, monkeypatch):
    _pv2, base, _parent = project
    from app.db import base as db_base
    real_transaction = db_base.transaction
    state = {"locked": False}

    class Watched:
        def __init__(self):
            self.inner = real_transaction()

        def __enter__(self):
            state["locked"] = True
            return self.inner.__enter__()

        def __exit__(self, *exc):
            state["locked"] = False
            return self.inner.__exit__(*exc)

    real = orch._start_checkout
    seen = []

    def record(sender, new_id):
        seen.append(state["locked"])
        return real(sender, new_id)

    monkeypatch.setattr(db_base, "transaction", Watched)
    monkeypatch.setattr(orch, "_start_checkout", record)
    args = {"title": "t", "prompt": "p", "request_id": "dup"}
    first = orch._queue("parent", args, "start")
    count = worktrees(base)
    again = orch._queue("parent", args, "start")
    assert again["status"] == "already_queued" and again["session_id"] == first["session_id"]
    assert seen == [False, False]
    assert worktrees(base) == count  # the retry's checkout was removed again


def test_a_chat_in_the_main_checkout_still_shares_it(project):
    pv2, _base, _parent = project
    chat("main-chat", pv2)
    result = orch._queue("main-chat", {"title": "t", "prompt": "p", "request_id": "main"}, "start")
    child = sessions.get_session(result["session_id"])
    assert not child["branch"] and not child["worktree_path"]
    assert "checkout" not in result and "main checkout" in result["note"]


def test_worker_branches_are_pushable_names_and_legacy_ones_still_count():
    from app.core import worker_checkout

    assert worker_checkout.worker_branch("w1") == "chore/task-w1"
    assert worker_checkout.chat_branch("0f2dbe68-4491") == "chore/chat-0f2dbe68"
    assert worker_checkout.is_worker_branch("chore/task-w1", "w1")
    assert worker_checkout.is_worker_branch("task/w1", "w1")
    assert not worker_checkout.is_worker_branch("task/w2", "w1")


def test_a_branch_name_origin_rules_refuse_gets_a_plain_rename_hint():
    from app.core import checkout_transfer

    reason = checkout_transfer._push_refused(
        b"remote: error: GH013: Repository rule violations found for refs/heads/task/x.", "task/x")
    assert "branch rules refuse the name task/x" in reason and "chore/" in reason
