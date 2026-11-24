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


@pytest.fixture
def trunk_repo(mock_db, tmp_path):
    """A project whose repository has no ``main``: its default branch is ``trunk``."""
    origin = tmp_path / "trunk-origin.git"
    git(tmp_path, "init", "--bare", "-b", "trunk", str(origin))
    seed = clone(origin, tmp_path / "trunk-seed")
    (seed / ".gitignore").write_text(".fluid/\n")
    git(seed, "add", ".gitignore")
    tip = commit(seed, "code.txt", "trunk work\n")
    git(seed, "push", "origin", "trunk")
    base = clone(origin, tmp_path / "trunk-base")
    project = projects_v2.upsert_project("local:trunk-base", name="Trunk")
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(base))
    return dict(project=project, base=base, origin=origin, tip=tip)


@pytest.mark.parametrize("asked", [None, "", "main"])
def test_new_chat_forks_from_the_real_default_when_main_is_missing(trunk_repo, asked):
    """Older composers ask for ``main`` when they don't know the default branch."""
    r = trunk_repo
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-trunk", base_branch=asked,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == r["tip"]


def test_new_chat_missing_main_falls_back_offline_too(trunk_repo, tmp_path):
    r = trunk_repo
    git(r["base"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-trunk-offline", base_branch="main",
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == r["tip"]


def _init_wip_repo(repo):
    """A repository on feature/wip with one commit; its own identity, as CI has no global one."""
    git(repo.parent, "init", "-b", "feature/wip", str(repo))
    git(repo, "config", "user.name", "Remote Test")
    git(repo, "config", "user.email", "remote@example.test")
    commit(repo, "a.txt", "a\n")
    return repo


def test_unknown_default_is_never_the_checked_out_branch(tmp_path):
    """No origin/HEAD and no main/master: the branch a checkout has out is not the default."""
    from app.core.git_service import git_service
    repo = _init_wip_repo(tmp_path / "plain")
    assert git_service.known_default_branch(str(repo)) is None
    assert git_service._get_default_branch_cli(str(repo)) == "main"


def test_new_chat_asks_origin_when_its_head_is_missing(trunk_repo):
    """A checkout on a feature branch with no recorded origin/HEAD forks from origin's default."""
    from app.core.git_cache import _git_cache
    r = trunk_repo
    git(r["base"], "checkout", "-q", "-b", "feature/wip")
    commit(r["base"], "wip.txt", "unrelated\n")
    git(r["base"], "remote", "set-head", "origin", "--delete")
    _git_cache.invalidate_prefix("default_branch:")
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-no-head", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == r["tip"]


def test_a_missing_picked_base_is_reported_not_replaced(trunk_repo):
    """Only the legacy ``main`` falls back; a branch the user picked that is gone fails."""
    r = trunk_repo
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-gone-base", base_branch="release/2",
                                            create_new_branch=True, provision_runtime=False)
    assert path is None


def _stale_master_checkout(r):
    """origin defaults to an unfetched ``trunk``; the checkout only knows a stale ``master``."""
    from app.core.git_cache import _git_cache
    git(r["base"], "branch", "-m", "trunk", "master")
    git(r["base"], "update-ref", "refs/remotes/origin/master", "HEAD")
    git(r["base"], "update-ref", "-d", "refs/remotes/origin/trunk")
    git(r["base"], "remote", "set-head", "origin", "--delete")
    _git_cache.invalidate_prefix("default_branch:")
    seed = clone(r["origin"], r["base"].parent / "trunk-seed-2")
    return commit_and_push(seed, "newer.txt")


def commit_and_push(repo, name):
    tip = commit(repo, name, "newer trunk work\n")
    git(repo, "push", "-q", "origin", "trunk")
    return tip


def test_origin_default_beats_a_stale_local_master(trunk_repo):
    """S1: no origin/HEAD and a cached master; origin's unfetched trunk is still the base."""
    r = trunk_repo
    tip = _stale_master_checkout(r)
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-stale-master", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == tip
    assert git(r["base"], "symbolic-ref", "--short", "refs/remotes/origin/HEAD") == "origin/trunk"


def test_unreachable_origin_falls_back_to_local_master(trunk_repo, tmp_path):
    """S1: only when origin cannot answer does a local main/master stand in."""
    r = trunk_repo
    _stale_master_checkout(r)
    git(r["base"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert checkout_manager._fork_default(str(r["base"])) == ("master", False)


def test_a_cached_remote_master_is_not_a_known_default(tmp_path):
    """S2: worktree cleanup must not treat a cached origin/master as the proven default."""
    from app.core.git_service import git_service
    repo = _init_wip_repo(tmp_path / "cached")
    git(repo, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    assert git_service.known_default_branch(str(repo)) is None
    assert git_service.known_default_branch(str(repo), confirmed=True) is None


def test_origin_named_default_that_cannot_be_fetched_is_reported(trunk_repo):
    """S1 r3: origin names release/stable, a stale origin/release blocks its ref: fail, not master."""
    r = trunk_repo
    _stale_master_checkout(r)
    seed = clone(r["origin"], r["base"].parent / "release-seed")
    git(seed, "checkout", "-q", "-b", "release/stable")
    git(seed, "push", "-q", "origin", "release/stable")
    git(r["origin"], "symbolic-ref", "HEAD", "refs/heads/release/stable")
    git(r["base"], "update-ref", "refs/remotes/origin/release", "HEAD")  # blocks origin/release/stable
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-blocked-default", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path is None


def test_learning_the_default_keeps_fetch_head(trunk_repo):
    """S5: the discovery fetch must not overwrite a FETCH_HEAD the developer made."""
    r = trunk_repo
    tip = _stale_master_checkout(r)
    seed = clone(r["origin"], r["base"].parent / "topic-seed")
    git(seed, "checkout", "-q", "-b", "topic/inspect")
    topic = commit(seed, "topic.txt", "topic\n")
    git(seed, "push", "-q", "origin", "topic/inspect")
    git(r["base"], "fetch", "-q", "origin", "topic/inspect")
    assert git(r["base"], "rev-parse", "FETCH_HEAD") == topic
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-keeps-fetch-head", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == tip
    assert git(r["base"], "rev-parse", "FETCH_HEAD") == topic


def test_unavailable_origin_default_is_named_in_the_error(trunk_repo):
    """S1 r4: callers that ask for errors get the branch name, not "not checked out"."""
    from app.core.git_worktree_ops import BaseBranchUnavailableError
    r = trunk_repo
    _stale_master_checkout(r)
    seed = clone(r["origin"], r["base"].parent / "release-seed-2")
    git(seed, "checkout", "-q", "-b", "release/stable")
    git(seed, "push", "-q", "origin", "release/stable")
    git(r["origin"], "symbolic-ref", "HEAD", "refs/heads/release/stable")
    git(r["base"], "update-ref", "refs/remotes/origin/release", "HEAD")  # blocks origin/release/stable
    with pytest.raises(BaseBranchUnavailableError, match="release/stable"):
        checkout_manager.ensure_checkout(r["project"], "chore/chat-named-error", base_branch=None,
                                         create_new_branch=True, provision_runtime=False,
                                         raise_add_errors=True)


@pytest.mark.parametrize("fetched", [True, False])
def test_switched_origin_does_not_reuse_the_old_default(trunk_repo, fetched):
    """S4 r4: set-url leaves origin/HEAD on the old repository's trunk; the new default wins."""
    from app.api import git_helpers
    from app.core.git_cache import _git_cache
    r = trunk_repo
    new_origin = r["base"].parent / "stable-origin.git"
    git(r["base"].parent, "init", "--bare", "-b", "stable", str(new_origin))
    seed = clone(r["origin"], r["base"].parent / "stable-seed")
    git(seed, "checkout", "-q", "-b", "stable")
    stable_tip = commit(seed, "stable.txt", "stable work\n")
    git(seed, "push", "-q", str(new_origin), "trunk", "stable")
    git(new_origin, "symbolic-ref", "HEAD", "refs/heads/stable")

    database.create_project("switched-origin", "Example", "", str(r["base"]), {})
    git_helpers._repo_record_cache.pop("switched-origin", None)
    assert git_helpers.ensure_git_repository_record("switched-origin", str(r["base"]))["default_branch"] == "trunk"

    git(r["base"], "remote", "set-url", "origin", str(new_origin))
    if fetched:
        git(r["base"], "fetch", "-q", "origin")
    assert git(r["base"], "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/trunk"
    git_helpers._repo_record_cache.pop("switched-origin", None)
    _git_cache.invalidate_prefix("default_branch:")
    record = git_helpers.ensure_git_repository_record("switched-origin", str(r["base"]))
    assert record["default_branch"] == ("stable" if fetched else None)

    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-switched-origin", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == stable_tip
    assert git(r["base"], "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/stable"


def test_chat_right_after_set_url_asks_the_new_origin(trunk_repo):
    """S4 r5: no status refresh between set-url and the chat; origin/HEAD still says trunk."""
    r = trunk_repo
    new_origin = r["base"].parent / "stable-origin-direct.git"
    git(r["base"].parent, "init", "--bare", "-b", "stable", str(new_origin))
    seed = clone(r["origin"], r["base"].parent / "stable-seed-direct")
    git(seed, "checkout", "-q", "-b", "stable")
    stable_tip = commit(seed, "stable.txt", "stable work\n")
    git(seed, "push", "-q", str(new_origin), "trunk", "stable")
    git(new_origin, "symbolic-ref", "HEAD", "refs/heads/stable")
    git(r["base"], "remote", "set-url", "origin", str(new_origin))
    assert git(r["base"], "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/trunk"

    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-direct-after-set-url", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == stable_tip


def test_unreachable_origin_uses_the_recorded_default(trunk_repo):
    """Offline, a recorded origin/HEAD is still the base (cached origin/trunk)."""
    r = trunk_repo
    r["origin"].rename(r["origin"].with_name("moved-away.git"))
    path = checkout_manager.ensure_checkout(r["project"], "chore/chat-offline-recorded", base_branch=None,
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == r["tip"]
