"""Fresh bases through real Git producers, including two-worker negotiation."""
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import checkout_manager, git_fork_base, worker_checkout
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME, pin_runner  # noqa: F401
from tests.core.test_remote_worker_checkout import clone, commit, git

TARGET = "fresh-target"


@pytest.fixture
def world(mock_db, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = clone(origin, tmp_path / "seed")
    (seed / ".gitignore").write_text(".fluid/\n")
    git(seed, "add", ".gitignore")
    old = commit(seed, "code.txt", "old\n")
    git(seed, "push", "origin", "main")
    base = clone(origin, tmp_path / "base")
    target = clone(origin, tmp_path / "target")
    caller = tmp_path / "caller"
    git(base, "worktree", "add", "-b", "caller", str(caller))
    git(caller, "branch", "--set-upstream-to=origin/main")
    fresh = commit(seed, "new.txt", "new main work\n")
    git(seed, "push", "origin", "main")
    project = projects_v2.upsert_project("local:fresh-base", name="Fresh base")
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(base))
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(caller), branch="caller")
    checkouts.upsert_checkout(project["id"], TARGET, str(target))
    database.create_profile("coder", "Coder", None, {"allowed_tools": ["Read", "Edit", "Bash"]})
    database.create_session("parent", "coder", project_v2_id=project["id"])
    database.set_session_branch("parent", "caller", project["id"])
    return dict(row=database.get_session("parent"), origin=origin, seed=seed, base=base,
                target=target, caller=caller, project=project, old=old, fresh=fresh)


def prepare(w, **kwargs):
    return worker_checkout.prepare_worker_checkout(w["row"], "fresh-test", "coder", **kwargs)




@pytest.mark.parametrize("tracks_default", [False, True])
def test_non_default_caller_only_refreshes_when_tracking_default(world, tracks_default):
    w = world
    git(w["caller"], "switch", "-c", "hotfix/1.2")
    checkouts.upsert_checkout(w["project"]["id"], LOCAL_ME, str(w["caller"]), branch="hotfix/1.2")
    database.set_session_branch("parent", "hotfix/1.2", w["project"]["id"])
    w["row"] = database.get_session("parent")
    if tracks_default:
        git(w["caller"], "branch", "--set-upstream-to=origin/main")
    checkout = prepare(w)
    assert checkout.base_commit == (w["fresh"] if tracks_default else w["old"])
    assert git(w["caller"], "rev-parse", "HEAD") == w["old"]


def test_explicit_base_does_not_fetch(world, monkeypatch):
    monkeypatch.setattr(git_fork_base, "fetch_base", lambda *a, **kw: pytest.fail("Explicit base must not fetch"))
    assert prepare(world, base_commit=world["old"]).base_commit == world["old"]


def test_chat_fetch_has_longer_deadline(world, monkeypatch):
    from app.core import checkout_transfer
    run = checkout_transfer._run
    timeouts = []

    def observed(cwd, *args, **kwargs):
        if args[0] == "fetch":
            timeouts.append(kwargs["timeout"])
        return run(cwd, *args, **kwargs)

    monkeypatch.setattr(checkout_transfer, "_run", observed)
    path = checkout_manager.ensure_checkout(world["project"], "chore/chat-deadline",
        create_new_branch=True, provision_runtime=False)
    assert path
    assert timeouts == [60]


def test_remote_hotfix_does_not_negotiate_fresh_default(world, monkeypatch):
    w = world
    git(w["caller"], "branch", "--unset-upstream")
    state = worker_checkout.publish_worker_branch(w["row"], "remote-hotfix", "coder", fresh_base=True)
    assert state.pushed, state.reason
    assert state.base_policy is None
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _: TARGET))
    received = worker_checkout.receive_worker_branch(state.to_wire(), "remote-hotfix")
    assert received.checkout.base_commit == w["old"]


def test_missing_dispatch_keeps_branch_with_honest_reason(world):
    w = world
    state = worker_checkout.publish_worker_branch(w["row"], "missing-dispatch", "coder", fresh_base=True)
    assert state.pushed, state.reason
    block = dict(mode=worker_checkout.REMOTE_BRANCH, branch=state.branch,
                 base=w["fresh"], head=w["fresh"], commits=0)
    outcome = worker_checkout.fetch_worker_commits(w["row"], "missing-dispatch", block)
    assert not outcome["released"]
    assert "dispatch" in outcome["reason"].lower()
    assert "moved past" not in outcome["reason"]
    assert git(w["origin"], "rev-parse", state.branch) == w["old"]


def test_behind_caller_gets_fresh_default_without_touching_caller(world):
    w = world
    (w["caller"] / "dirty.txt").write_text("keep me")
    before = git(w["caller"], "status", "--porcelain")
    checkout = prepare(w)
    assert checkout.base_commit == w["fresh"] == git(checkout.path, "rev-parse", "HEAD")
    assert "Fresh origin/main." in checkout.instructions()
    assert git(w["caller"], "rev-parse", "HEAD") == w["old"]
    assert git(w["caller"], "branch", "--show-current") == "caller"
    assert git(w["caller"], "status", "--porcelain") == before


def test_unique_caller_commits_stay_and_report_distance(world):
    w = world
    own = commit(w["caller"], "own.txt", "unique work")
    checkout = prepare(w)
    assert checkout.base_commit == own == git(checkout.path, "rev-parse", "HEAD")
    assert "Caller HEAD; 1 commit behind origin/main." in checkout.instructions()
    assert git(w["caller"], "rev-parse", "HEAD") == own


def test_explicit_base_wins_even_when_caller_is_only_behind(world):
    checkout = prepare(world, base_commit=world["old"])
    assert checkout.base_commit == world["old"]
    assert git(checkout.path, "rev-parse", "HEAD") == world["old"]
    assert "Explicit base_commit." in checkout.instructions()
    assert git(world["caller"], "rev-parse", "origin/main") == world["old"]


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_or_timed_out_fetch_falls_back(world, monkeypatch, tmp_path, caplog, timeout):
    w = world
    if timeout:
        slow = tmp_path / "slow.py"
        slow.write_text("import time; time.sleep(10)\n")
        argv = [sys.executable, str(slow)]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        git(w["base"], "config", "remote.origin.uploadpack", command)
        monkeypatch.setattr(git_fork_base, "FETCH_TIMEOUT", 0.05)
    else:
        git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    started = time.monotonic()
    checkout = prepare(w)
    assert time.monotonic() - started < 6, "Dispatch must not wait for the ten-second child"
    assert checkout.base_commit == w["old"] == git(checkout.path, "rev-parse", "HEAD")
    assert "Caller HEAD; fetch-failed fallback." in checkout.instructions()
    if timeout:
        assert "Checkout base fetch failed or timed out" in caplog.text


def test_fetch_timeout_kills_real_credential_helper_without_pipe_drain(world, monkeypatch, tmp_path):
    import http.server
    import json
    import threading
    import psutil

    class Unauthorized(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.end_headers()

        def log_message(self, *args):
            pass

    w = world
    marker = tmp_path / "helper-started.json"
    helper = tmp_path / "slow-helper.py"
    helper.write_text(
        "import os, json, time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(), "
        "terminal=os.environ.get('GIT_TERMINAL_PROMPT'), "
        "interactive=os.environ.get('GCM_INTERACTIVE'))))\n"
        "time.sleep(15)\n"
    )
    git(w["base"], "config", "credential.helper", "")
    git(w["base"], "config", "--add", "credential.helper",
        f'!"{sys.executable.replace(os.sep, "/")}" "{helper.as_posix()}"')
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GCM_INTERACTIVE", "Always")
    monkeypatch.setattr(git_fork_base, "FETCH_TIMEOUT", 3)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        git(w["base"], "remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/repo.git")
        started = time.monotonic()
        checkout = prepare(w)
        assert time.monotonic() - started < 9, "Do not drain pipes held by the fifteen-second helper"
        assert checkout.base_commit == w["old"]
        assert marker.exists(), "The real Git credential helper must have started"
        recorded = json.loads(marker.read_text())
        assert recorded["terminal"] == "0"
        assert recorded["interactive"] == "Never"
        try:
            helper_process = psutil.Process(recorded["pid"])
            deadline = time.monotonic() + 2
            while helper_process.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
                time.sleep(0.01)
            assert helper_process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if marker.exists():
            try:
                psutil.Process(json.loads(marker.read_text())["pid"]).kill()
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("explicit_branch", [False, True])
def test_new_chat_fetches_before_forking(world, explicit_branch):
    w = world
    kwargs = {"base_branch": "main"} if explicit_branch else {}
    path = checkout_manager.ensure_checkout(w["project"], "chore/chat-fresh", create_new_branch=True,
                                            provision_runtime=False, **kwargs)
    assert path and git(path, "rev-parse", "HEAD") == w["fresh"]
    assert git(w["base"], "rev-parse", "HEAD") == w["old"]


def test_new_chat_fetch_failure_keeps_cached_base(world, tmp_path):
    w = world
    git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    path = checkout_manager.ensure_checkout(w["project"], "chore/chat-offline", base_branch="main",
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == w["old"]


def test_remote_default_fetch_failure_falls_back_after_task_branch_fetch(world, monkeypatch):
    w = world
    state = worker_checkout.publish_worker_branch(w["row"], "remote-offline", "coder", fresh_base=True)
    assert state.pushed, state.reason
    # Task publication remains fetchable, but the default branch is unavailable.
    git(w["origin"], "update-ref", "-d", "refs/heads/main")
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _: TARGET))
    received = worker_checkout.receive_worker_branch(state.to_wire(), "remote-offline")
    assert received.checkout.base_commit == w["old"] == git(received.cwd, "rev-parse", "HEAD")
    assert "Caller HEAD; fetch-failed fallback." in received.checkout.instructions()


def test_existing_checkout_fast_path_never_takes_project_lock(world, monkeypatch):
    w = world

    def refuse_lock(*args):
        pytest.fail("Existing checkout lookup must not acquire the project lock")

    monkeypatch.setattr(worker_checkout, "_project_lock", refuse_lock)
    path = checkout_manager.ensure_checkout(w["project"], "caller", provision_runtime=False)
    assert path == str(w["caller"])


def test_offline_chat_without_base_uses_cached_default_not_base_head(world, tmp_path):
    w = world
    git(w["base"], "switch", "-c", "other-branch")
    own = commit(w["base"], "other.txt", "not the default branch")
    git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    path = checkout_manager.ensure_checkout(w["project"], "chore/chat-offline-default",
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == w["old"] != own
    assert git(w["base"], "rev-parse", "HEAD") == own


def test_fetch_coalesces_worktrees_then_refreshes_after_ten_seconds(world, monkeypatch):
    from app.core import checkout_transfer

    w = world
    run = checkout_transfer._run
    fetches = []

    def observed_run(cwd, *args, **kwargs):
        if args[0] == "fetch":
            fetches.append(args)
        return run(cwd, *args, **kwargs)

    monkeypatch.setattr(checkout_transfer, "_run", observed_run)
    assert git_fork_base.fetch_base(str(w["base"])) == ("main", True)
    fresh = commit(w["seed"], "later.txt", "newer default")
    git(w["seed"], "push", "origin", "main")
    assert git_fork_base.fetch_base(str(w["caller"])) == ("main", True)
    assert len(fetches) == 1
    assert git(w["base"], "rev-parse", "origin/main") == w["fresh"]
    key = (str(w["base"].resolve()), "main")
    git_fork_base._recent_fetches[key] -= 11
    assert git_fork_base.fetch_base(str(w["caller"])) == ("main", True)
    assert len(fetches) == 2
    assert git(w["base"], "rev-parse", "origin/main") == fresh
    # A different branch never shares the freshness window.
    git(w["seed"], "push", "origin", "HEAD:refs/heads/topic")
    assert git_fork_base.fetch_base(str(w["caller"]), "topic") == ("topic", True)
    assert len(fetches) == 3


def test_failed_fetch_is_not_coalesced(world, tmp_path):
    w = world
    git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert git_fork_base.fetch_base(str(w["base"])) == ("main", False)
    git(w["base"], "remote", "set-url", "origin", str(w["origin"]))
    assert git_fork_base.fetch_base(str(w["caller"])) == ("main", True)
    assert git(w["base"], "rev-parse", "origin/main") == w["fresh"]


def test_old_git_uses_compatible_flags_and_the_shared_repository_lock(world, monkeypatch, caplog):
    from app.core import checkout_transfer
    from app.core.git_worktree_ops import repo_worktree_lock

    w = world
    run = checkout_transfer._run
    locks = []
    commands = []

    def observed_lock(root):
        locks.append(root)
        return repo_worktree_lock(root)

    def observed_run(cwd, *args, **kwargs):
        commands.append(args)
        return run(cwd, *args, **kwargs)

    # Feature gating only: all repository operations still run real Git.
    monkeypatch.setattr(git_fork_base, "_git_version", lambda _: (2, 25))
    monkeypatch.setattr(git_fork_base, "FETCH_COALESCE_SECONDS", 0)
    monkeypatch.setattr(git_fork_base, "repo_worktree_lock", observed_lock)
    monkeypatch.setattr(checkout_transfer, "_run", observed_run)
    git_fork_base._log_legacy_fetch.cache_clear()
    for _ in range(2):
        assert git_fork_base.fetch_base(str(w["caller"])) == ("main", True)
    assert locks == [str(w["base"].resolve())] * 2
    assert all("--no-write-fetch-head" not in command for command in commands)
    assert caplog.text.count("Git older than 2.29") == 1
    assert git(w["caller"], "rev-parse", "origin/main") == w["fresh"]


@pytest.mark.parametrize("new_peer", [False, True])
@pytest.mark.parametrize("own_commit", [False, True])
def test_remote_receiver_fetches_only_for_negotiated_policy(world, monkeypatch, new_peer, own_commit):
    w = world
    caller = commit(w["caller"], "own.txt", "caller work") if own_commit else w["old"]
    state = worker_checkout.publish_worker_branch(w["row"], "remote-fresh", "coder", fresh_base=new_peer)
    assert state.pushed, state.reason
    wire = state.to_wire()
    assert ("base_policy" in wire) == new_peer
    # Advance origin AFTER publication: the receiving worker must do its own fetch.
    fresh = commit(w["seed"], "later.txt", "later main")
    git(w["seed"], "push", "origin", "main")
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _: TARGET))
    received = worker_checkout.receive_worker_branch(wire, "remote-fresh")
    expected = fresh if new_peer and not own_commit else caller
    assert received.checkout.base_commit == expected == git(received.cwd, "rev-parse", "HEAD")
    if new_peer:
        assert git(w["target"], "rev-parse", "origin/main") == fresh
        note = "Caller HEAD; 2 commits behind origin/main." if own_commit else "Fresh origin/main."
        assert note in received.checkout.instructions()
    else:
        assert git(w["target"], "rev-parse", "origin/main") == w["old"]
    assert git(w["target"], "rev-parse", "HEAD") == w["old"]
    assert git(w["caller"], "rev-parse", "HEAD") == caller
