"""Ship It / Finish / operation polling / pr-state end to end.

Real repositories under ``tmp_path`` (pushes land in a local bare repo), a stub
``gh`` executable on PATH (``tests/gh_stub.py``) and a scripted model behind
the provider adapter seam (``tests/git_flow_helpers.FakeModel``). Nothing in
``git_service`` / ``github_service`` / ``git_operations`` is mocked, so each
test pins what the docs promise (docs/content/api-reference/miscellaneous.md,
"Ship It", "Finish", "operations", "pr-state") on the real control flow — and
what state a failure at each step leaves behind.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import git_operations
from app.core import config
from app.core.environment import ProviderConfigError
from app.db import database
from tests.gh_stub import FakeGh, pr_json
from tests.git_flow_helpers import (
    REPO,
    bare_of,
    build_git_app,
    github_repo,
    remote_has_branch,
    reset_git_state,
    steps,
    wait_op,
)
from tests.git_repo_helpers import commit_file, git, hermetic_git_env, write

BASE = "/api/v1/projects/p1/git"
PR_URL = f"https://github.com/{REPO}/pull/7"
SHIP_STEPS = ["commit", "push", "create_pr", "merge", "cleanup"]
FINISH_STEPS = ["guard", "push", "merge", "cleanup"]
PR_JSON_REPLY = '{"title": "Add the feature", "body": "## Summary\\n- adds feature"}'


@pytest.fixture
def env(mock_db, tmp_path, monkeypatch):
    from tests.git_flow_helpers import FakeModel

    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    reset_git_state()
    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    model = FakeModel().install(monkeypatch)
    repo = github_repo(tmp_path)
    database.create_project("p1", "Repo", None, str(repo))
    with TestClient(build_git_app()) as client:
        yield SimpleNamespace(client=client, repo=repo, gh=gh, model=model, tmp=tmp_path)
    reset_git_state()


def _no_open_pr(gh):
    gh.on("pr", "list", stdout=[])


def _pr_created(gh, number=7, head="feat/x"):
    gh.on("pr", "create", stdout=f"https://github.com/{REPO}/pull/{number}\n")
    gh.on("pr", "view", str(number), stdout=pr_json(number, head, title="Add the feature"))


def _ship(env, **body):
    response = env.client.post(f"{BASE}/ship", json=body)
    assert response.status_code == 200, response.text
    first = response.json()
    assert set(first) == {"operation_id", "status", "steps", "result"}
    assert first["status"] == "running"
    assert [s["id"] for s in first["steps"]] == SHIP_STEPS
    return wait_op(env.client, "p1", first["operation_id"])


def _finish(env, **body):
    response = env.client.post(f"{BASE}/finish", json=body)
    assert response.status_code == 200, response.text
    first = response.json()
    assert [s["id"] for s in first["steps"]] == FINISH_STEPS
    assert steps(first)["guard"]["status"] == "done"
    return wait_op(env.client, "p1", first["operation_id"])


def _head(repo):
    return git(repo, "rev-parse", "HEAD")


# =============================================================================
# Ship It
# =============================================================================


def test_ship_commits_pushes_opens_and_merges(env):
    write(env.repo / "feature.txt", "feature\nmore\n")
    write(env.repo / "new.txt", "new\n")
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7")
    env.model.queue = ["```\nfeat: ship the feature\n```", PR_JSON_REPLY]

    done = _ship(env, merge_method="rebase")

    assert done["status"] == "done", done
    by_id = steps(done)
    assert [by_id[s]["status"] for s in SHIP_STEPS] == ["done", "done", "done", "done", "skipped"]
    # Fence stripped before the message reached `git commit -m`.
    assert by_id["commit"]["detail"] == "feat: ship the feature"
    assert git(env.repo, "log", "-1", "--format=%B").strip() == "feat: ship the feature"
    assert git(env.repo, "status", "--porcelain") == ""
    assert by_id["create_pr"]["detail"] == PR_URL
    assert done["result"] == {"pr_number": 7, "pr_url": PR_URL}
    # The pushed tip is the new commit.
    assert git(bare_of(env.repo), "rev-parse", "feat/x") == _head(env.repo)

    [create] = env.gh.calls("pr", "create")
    assert create["args"] == [
        "pr", "create", "--repo", REPO, "--title", "Add the feature",
        "--body-file", "-", "--head", "feat/x", "--base", "main",
    ]
    assert create["stdin"] == "## Summary\n- adds feature"
    assert env.gh.argvs("pr", "merge") == [
        ["pr", "merge", "7", "--repo", REPO, "--rebase", "--delete-branch"]
    ]
    # The commit prompt carried the real diff (and names the new file, which
    # `git diff` alone never shows); the PR prompt the branch log.
    assert "+more" in env.model.prompts[0] and "new file: new.txt" in env.model.prompts[0]
    assert "feat: ship the feature" in env.model.prompts[1]
    assert git(env.repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "feature.txt", "new.txt",
    ]


def test_ship_with_only_new_files_commits_them(env):
    """Regression: untracked-only trees failed Ship It with "No changes to
    generate a commit message for" although the tree is dirty and `git add .`
    would have committed them."""
    write(env.repo / "brand_new.py", "print('hi')\n")
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7")
    env.model.queue = ["feat: add brand_new", PR_JSON_REPLY]

    done = _ship(env)

    assert done["status"] == "done", done
    assert steps(done)["commit"]["detail"] == "feat: add brand_new"
    assert "new file: brand_new.py" in env.model.prompts[0]
    assert git(env.repo, "show", "--name-only", "--format=", "HEAD") == "brand_new.py"


def test_ship_model_output_is_data_never_git_or_gh_flags(env):
    """Hostile model output lands as commit/PR text, not as argv options, and
    the prompt (diff, instructions) never becomes part of a git/gh argument."""
    write(env.repo / "feature.txt", "SECRET_DIFF_LINE\n")
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7")
    env.model.queue = [
        "--amend --no-edit\n:wq",
        '{"title": "--base evil\\n--draft", "body": "(no output)\\n--web"}',
    ]
    before = _head(env.repo)

    done = _ship(env)

    assert done["status"] == "done", done
    # A new commit (not an amend) whose message is the literal text, with the
    # vim artifact stripped.
    assert git(env.repo, "rev-parse", "HEAD~1") == before
    assert git(env.repo, "log", "-1", "--format=%B").strip() == "--amend --no-edit"
    [call] = env.gh.calls("pr", "create")
    argv = call["args"]
    assert argv[argv.index("--title") + 1] == "--base evil --draft"
    assert argv[argv.index("--base") + 1] == "main"
    assert "--draft" not in argv and "--web" not in argv
    assert call["stdin"] == "--web"
    assert not any("SECRET_DIFF_LINE" in a for c in env.gh.calls() for a in c["args"])
    assert "SECRET_DIFF_LINE" not in (call["stdin"] or "")


def test_ship_draft_and_explicit_base_reach_gh(env):
    git(env.repo, "branch", "develop", "main")
    git(env.repo, "push", "-q", "origin", "develop")
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7")
    env.model.reply = PR_JSON_REPLY

    done = _ship(env, base_branch="develop", draft=True)

    assert done["status"] == "done", done
    # Clean tree: nothing to commit, but the never-pushed branch is pushed.
    assert steps(done)["commit"] == {"id": "commit", "label": "Commit changes",
                                     "status": "skipped", "detail": "Nothing to commit"}
    assert steps(done)["push"]["status"] == "done"
    [argv] = env.gh.argvs("pr", "create")
    assert argv[-3:] == ["--base", "develop", "--draft"]
    # Default merge method is squash.
    assert "--squash" in env.gh.argvs("pr", "merge")[0]


def test_ship_commit_failure_leaves_tree_dirty_and_nothing_pushed(env):
    write(env.repo / "feature.txt", "changed\n")
    _no_open_pr(env.gh)
    env.model.config_error = ProviderConfigError("No text-generation provider is configured")
    before = _head(env.repo)

    done = _ship(env)

    assert done["status"] == "error"
    by_id = steps(done)
    assert by_id["commit"]["status"] == "error"
    assert "No text-generation provider is configured" in by_id["commit"]["error"]
    assert [by_id[s]["status"] for s in SHIP_STEPS[1:]] == ["pending"] * 4
    assert done["result"] is None
    assert _head(env.repo) == before
    assert "feature.txt" in git(env.repo, "status", "--porcelain")
    assert not remote_has_branch(env.repo, "feat/x")
    assert env.gh.calls("pr", "create") == []


def test_ship_push_failure_keeps_local_commit_and_opens_no_pr(env):
    write(env.repo / "feature.txt", "changed\n")
    _no_open_pr(env.gh)
    git(env.repo, "config", f"url.{env.tmp / 'missing.git'}.pushInsteadOf",
        "https://github.com/acme/app.git")
    git(env.repo, "config", "--unset", f"url.{bare_of(env.repo)}.pushInsteadOf")
    env.model.reply = "fix: change the feature"

    done = _ship(env)

    assert done["status"] == "error"
    by_id = steps(done)
    assert by_id["commit"]["status"] == "done"
    assert by_id["push"]["status"] == "error" and by_id["push"]["error"]
    assert by_id["create_pr"]["status"] == "pending"
    # The commit stays local; no PR was attempted.
    assert git(env.repo, "log", "-1", "--format=%s") == "fix: change the feature"
    assert git(env.repo, "status", "--porcelain") == ""
    assert not remote_has_branch(env.repo, "feat/x")
    assert env.gh.calls("pr", "create") == []


def test_ship_create_pr_failure_leaves_branch_pushed(env):
    _no_open_pr(env.gh)
    env.gh.on("pr", "create", code=1, stderr="GraphQL: Head sha can't be blank (createPullRequest)")
    env.model.reply = PR_JSON_REPLY

    done = _ship(env)

    assert done["status"] == "error"
    by_id = steps(done)
    assert by_id["push"]["status"] == "done"
    assert by_id["create_pr"]["error"] == "GraphQL: Head sha can't be blank (createPullRequest)"
    assert by_id["merge"]["status"] == "pending"
    assert remote_has_branch(env.repo, "feat/x")
    assert env.gh.calls("pr", "merge") == []


def test_ship_merge_conflict_leaves_pr_open_then_retry_reuses_it(env):
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7", code=1,
              stderr="Pull request acme/app#7 is not mergeable: the merge commit cannot be cleanly created.")
    env.model.reply = PR_JSON_REPLY

    done = _ship(env)

    assert done["status"] == "error"
    by_id = steps(done)
    assert by_id["create_pr"]["status"] == "done"
    assert by_id["merge"]["status"] == "error"
    assert "not mergeable" in by_id["merge"]["error"]
    assert by_id["cleanup"]["status"] == "pending"
    # The PR number is reported even though the flow stopped.
    assert done["result"] == {"pr_number": 7, "pr_url": PR_URL}

    # Retry once the conflict is resolved: the open PR is reused, nothing is
    # committed or pushed again, and no second PR is created.
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])
    env.gh.on("pr", "merge", "7")
    again = _ship(env)

    assert again["status"] == "done", again
    by_id = steps(again)
    assert by_id["commit"]["status"] == "skipped"
    assert by_id["push"] == {"id": "push", "label": "Push commits",
                             "status": "skipped", "detail": "Branch already pushed"}
    assert by_id["create_pr"]["detail"] == "PR #7 already exists"
    assert len(env.gh.calls("pr", "create")) == 1


def test_ship_already_merged_counts_as_success(env):
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])
    env.gh.on("pr", "merge", "7", code=1, stderr="Pull request #7 was already merged")

    done = _ship(env)

    assert done["status"] == "done"
    assert steps(done)["merge"] == {"id": "merge", "label": "Merge pull request",
                                    "status": "done", "detail": "Already merged"}


def test_ship_cleanup_returns_to_default_branch_and_deletes_feature(env):
    _no_open_pr(env.gh)
    _pr_created(env.gh)
    env.gh.on("pr", "merge", "7")
    env.model.reply = PR_JSON_REPLY

    done = _ship(env, cleanup=True)

    assert done["status"] == "done", done
    assert steps(done)["cleanup"]["status"] == "done"
    assert git(env.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(env.repo, "branch", "--list", "feat/x") == ""


def test_ship_guards(env):
    client = env.client
    # Current branch is the base branch.
    git(env.repo, "checkout", "-q", "main")
    reset_git_state()
    env.gh.signed_in()
    response = client.post(f"{BASE}/ship", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Current branch 'main' is the base branch — switch to a feature branch first"
    )
    git(env.repo, "checkout", "-q", "feat/x")

    # GitHub unreachable: 502, never "no PR" (which would open a duplicate).
    env.gh.on("pr", "list", code=1, stderr="error connecting to api.github.com")
    response = client.post(f"{BASE}/ship", json={})
    assert response.status_code == 502
    assert "error connecting to api.github.com" in response.json()["detail"]

    # A failed guard unregisters its placeholder op: nothing blocks the next one.
    assert git_operations._operations == {}

    # No GitHub remote.
    git(env.repo, "remote", "set-url", "origin", str(bare_of(env.repo)))
    response = client.post(f"{BASE}/ship", json={})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Could not determine GitHub repository")

    # Invalid merge method is rejected by validation before any git work.
    assert client.post(f"{BASE}/ship", json={"merge_method": "admin"}).status_code == 422


def _assert_unreachable_github_is_502(env):
    env.gh.on("pr", "list", code=1, stderr="error connecting to api.github.com")
    env.gh.on("pr", "create", stdout=f"https://github.com/{REPO}/pull/8\n")
    env.gh.on("pr", "view", "8", stdout=pr_json(8, "feat/x"))
    env.gh.on("pr", "merge", "8")
    env.model.reply = PR_JSON_REPLY
    response = env.client.post(f"{BASE}/ship", json={})
    assert response.status_code == 502, response.text
    assert env.gh.calls("pr", "create") == []


def test_unreachable_github_is_502_not_a_duplicate_pr(env):
    _assert_unreachable_github_is_502(env)


def test_unreachable_guard_bites_when_disabled(env, monkeypatch):
    """Degrade "could not ask" to "no PR" and Ship opens a duplicate PR."""
    real = git_operations._find_open_pr_for_branch

    def swallow(repo, branch):
        try:
            return real(repo, branch)
        except git_operations.GitHubCliError:
            return None

    monkeypatch.setattr(git_operations, "_find_open_pr_for_branch", swallow)
    with pytest.raises(AssertionError):
        _assert_unreachable_github_is_502(env)


def _option_branch_ship(env):
    """Ship from a branch literally named ``--mirror`` (reachable through
    ``git init --initial-branch`` or a cloned remote HEAD)."""
    git(env.repo, "push", "-q", "origin", "main:keep-me")
    git(env.repo, "update-ref", "refs/heads/--mirror", "HEAD")
    git(env.repo, "symbolic-ref", "HEAD", "refs/heads/--mirror")
    reset_git_state()
    env.gh.signed_in()
    _no_open_pr(env.gh)
    _pr_created(env.gh, head="--mirror")
    env.gh.on("pr", "merge", "7")
    env.model.reply = PR_JSON_REPLY

    response = env.client.post(f"{BASE}/ship", json={})
    assert response.status_code == 200, response.text
    return wait_op(env.client, "p1", response.json()["operation_id"])


def _assert_branch_pushed_as_a_ref(env, done):
    assert steps(done)["push"]["status"] == "done", done
    # A mirror push would have deleted every remote branch the checkout lacks.
    assert remote_has_branch(env.repo, "keep-me")
    assert git(bare_of(env.repo), "rev-parse", "refs/heads/--mirror") == _head(env.repo)


def test_option_like_branch_is_pushed_as_a_ref_not_a_flag(env):
    done = _option_branch_ship(env)
    _assert_branch_pushed_as_a_ref(env, done)
    assert done["status"] == "done"
    # gh receives the name as the value of --head, never as a flag.
    [argv] = env.gh.argvs("pr", "create")
    assert argv[argv.index("--head") + 1] == "--mirror"


def test_option_like_branch_guard_bites_when_disabled(env, monkeypatch):
    from app.core import git_commit_ops

    monkeypatch.setattr(git_commit_ops, "_push_branch_arg", lambda branch: branch)
    done = _option_branch_ship(env)
    with pytest.raises(AssertionError):
        _assert_branch_pushed_as_a_ref(env, done)


def test_ship_not_a_repo_is_400(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    database.create_project("p-plain", "Plain", None, str(plain))
    response = env.client.post("/api/v1/projects/p-plain/git/ship", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "Project is not a git repository"


def test_second_operation_while_one_runs_is_409(env):
    op = git_operations.Operation(project_id="p1", kind="ship", steps=[])
    git_operations._register(op)

    for path in ("ship", "finish"):
        response = env.client.post(f"{BASE}/{path}", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == "Another git operation is already running for this project"


# =============================================================================
# Finish
# =============================================================================


def _pushed_with_open_pr(env):
    git(env.repo, "push", "-q", "-u", "origin", "feat/x")
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])


def test_finish_pushes_new_commits_and_merges(env):
    _pushed_with_open_pr(env)
    commit_file(env.repo, "later.txt", "later\n", "fix: follow-up")
    env.gh.on("pr", "merge", "7")

    done = _finish(env)

    assert done["status"] == "done", done
    by_id = steps(done)
    assert by_id["push"]["status"] == "done"
    assert by_id["merge"]["status"] == "done" and by_id["cleanup"]["status"] == "skipped"
    assert done["result"] == {"pr_number": 7, "pr_url": PR_URL}
    assert git(bare_of(env.repo), "rev-parse", "feat/x") == _head(env.repo)
    assert env.gh.argvs("pr", "merge") == [
        ["pr", "merge", "7", "--repo", REPO, "--squash", "--delete-branch"]
    ]


def test_finish_is_idempotent_after_merge(env):
    _pushed_with_open_pr(env)
    env.gh.on("pr", "merge", "7")
    assert _finish(env)["status"] == "done"

    # The PR is merged now: a second Finish is refused up front, not re-run.
    env.gh.on("pr", "list", stdout=[])
    response = env.client.post(f"{BASE}/finish", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "No open pull request found for branch 'feat/x'"
    assert len(env.gh.calls("pr", "merge")) == 1

    # Racing a merge that already landed on GitHub is still a success.
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])
    env.gh.on("pr", "merge", "7", code=1, stderr="Pull request #7 has already been merged")
    done = _finish(env)
    assert done["status"] == "done"
    assert steps(done)["merge"]["detail"] == "Already merged"


def test_finish_guards(env):
    client = env.client
    # A PR for another branch does not count.
    env.gh.on("pr", "list", stdout=[pr_json(9, "feat/other")])
    response = client.post(f"{BASE}/finish", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "No open pull request found for branch 'feat/x'"

    _pushed_with_open_pr(env)
    write(env.repo / "dirty.txt", "x\n")
    response = client.post(f"{BASE}/finish", json={})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Working tree has uncommitted changes")
    (env.repo / "dirty.txt").unlink()

    env.gh.on("pr", "list", code=1, stderr="HTTP 503: Service Unavailable")
    response = client.post(f"{BASE}/finish", json={})
    assert response.status_code == 502
    assert "HTTP 503" in response.json()["detail"]
    assert git_operations._operations == {}
    assert env.gh.calls("pr", "merge") == []


def _assert_dirty_finish_refused(env):
    _pushed_with_open_pr(env)
    env.gh.on("pr", "merge", "7")
    write(env.repo / "feature.txt", "uncommitted edit\n")
    response = env.client.post(f"{BASE}/finish", json={})
    assert response.status_code == 400, response.text
    assert env.gh.calls("pr", "merge") == []


def test_finish_refuses_dirty_tree_before_merging(env):
    _assert_dirty_finish_refused(env)


def test_finish_dirty_guard_bites_when_disabled(env, monkeypatch):
    """Report a clean tree and Finish merges without the uncommitted edit."""
    real = git_operations.git_service.get_status

    def always_clean(working_dir):
        return {**real(working_dir), "is_clean": True}

    monkeypatch.setattr(git_operations.git_service, "get_status", always_clean)
    with pytest.raises(AssertionError):
        _assert_dirty_finish_refused(env)
    for op_id in list(git_operations._operations):
        wait_op(env.client, "p1", op_id)
    assert len(env.gh.calls("pr", "merge")) == 1


def test_finish_merge_failure_is_reported_on_the_step(env):
    _pushed_with_open_pr(env)
    env.gh.on("pr", "merge", "7", code=1, stderr="GraphQL: Pull Request is not mergeable")

    done = _finish(env, merge_method="merge", cleanup=True)

    assert done["status"] == "error"
    by_id = steps(done)
    assert by_id["merge"]["error"] == "GraphQL: Pull Request is not mergeable"
    assert by_id["cleanup"]["status"] == "pending"
    # Nothing was cleaned up: still on the feature branch.
    assert git(env.repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/x"
    assert "--merge" in env.gh.argvs("pr", "merge")[0]


# =============================================================================
# Operation polling
# =============================================================================


def test_operation_unknown_wrong_project_wrong_session_and_expired(env, monkeypatch):
    client = env.client
    assert client.get(f"{BASE}/operations/nope").status_code == 404
    assert client.get(f"{BASE}/operations/nope").json()["detail"] == "Operation not found"

    _pushed_with_open_pr(env)
    env.gh.on("pr", "merge", "7")
    done = _finish(env)
    op_id = done["operation_id"]

    # Terminal snapshot is stable across polls.
    assert client.get(f"{BASE}/operations/{op_id}").json() == done
    database.create_project("p2", "Other", None, str(env.repo))
    assert client.get(f"/api/v1/projects/p2/git/operations/{op_id}").status_code == 404

    # Finished operations are kept for 10 minutes, then swept on the next op.
    git_operations._operations[op_id].created_at -= git_operations._OP_TTL_SECONDS + 1
    git_operations._sweep_expired()
    assert client.get(f"{BASE}/operations/{op_id}").status_code == 404


def test_operation_session_filter(env):
    op = git_operations.Operation(project_id="p1", kind="finish", steps=[], session_id="s-1")
    op.status = "done"
    git_operations._register(op)
    # No session filter: visible. Matching session is exercised by the
    # session router elsewhere; a mismatching one is hidden.
    assert env.client.get(f"{BASE}/operations/{op.id}").status_code == 200
    assert env.client.get(f"{BASE}/operations/{op.id}", params={"session_id": "s-2"}).status_code == 404


def test_running_operations_are_never_swept():
    op = git_operations.Operation(project_id="p1", kind="ship", steps=[])
    op.created_at -= git_operations._OP_TTL_SECONDS * 10
    git_operations._register(op)
    try:
        git_operations._sweep_expired()
        assert op.id in git_operations._operations
    finally:
        git_operations._operations.clear()


# =============================================================================
# pr-state
# =============================================================================


def test_pr_state_live_then_cached(env):
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x", mergeable="UNKNOWN", title="Add it")])

    first = env.client.get(f"{BASE}/pr-state")

    assert first.status_code == 200
    assert first.json() == {
        "current_branch": "feat/x",
        "has_github": True,
        "open_pr": {"number": 7, "title": "Add it", "url": PR_URL, "draft": False,
                    "state": "OPEN", "mergeable": None},
        "merged_head_sha": None,
        "pr_lookup_failed": False,
        "stale": False,
    }
    calls = len(env.gh.calls("pr", "list"))
    # Second poll within the TTL is served from cache: no gh call.
    assert env.client.get(f"{BASE}/pr-state").json() == first.json()
    assert len(env.gh.calls("pr", "list")) == calls


def test_pr_state_merged_head_when_no_open_pr(env):
    env.gh.on("pr", "list", "--repo", REPO, "--state", "open", stdout=[])
    env.gh.on("pr", "list", "--repo", REPO, "--state", "merged",
              stdout=[pr_json(5, "feat/x", state="MERGED", head_sha="a" * 40)])

    data = env.client.get(f"{BASE}/pr-state").json()

    assert data["open_pr"] is None and data["merged_head_sha"] == "a" * 40
    [merged_call] = env.gh.argvs("pr", "list", "--repo", REPO, "--state", "merged")
    assert merged_call[-2:] == ["--head", "feat/x"]


def test_pr_state_not_signed_in_is_has_github_false(env):
    # Documented: has_github is false without a signed-in GitHub CLI (not 401).
    env.gh.signed_in(False)

    response = env.client.get(f"{BASE}/pr-state")

    assert response.status_code == 200
    assert response.json()["has_github"] is False and response.json()["open_pr"] is None
    assert env.gh.calls("pr") == []
    # Not cached: signing in is visible on the next poll.
    env.gh.signed_in(True)
    reset_git_state()
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])
    assert env.client.get(f"{BASE}/pr-state").json()["open_pr"]["number"] == 7


def test_pr_state_lookup_failure_serves_last_known_answer_as_stale(env):
    env.gh.on("pr", "list", stdout=[pr_json(7, "feat/x")])
    assert env.client.get(f"{BASE}/pr-state").json()["stale"] is False
    for entry in git_operations._pr_state_cache._entries.values():
        entry.expires_at = 0.0  # past the TTL, still inside the stale window
    env.gh.on("pr", "list", code=1, stderr="API rate limit exceeded")
    data = env.client.get(f"{BASE}/pr-state").json()

    assert data["pr_lookup_failed"] is True and data["stale"] is True
    assert data["open_pr"]["number"] == 7


def test_pr_state_non_repo_and_no_remote(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    database.create_project("p-plain", "Plain", None, str(plain))
    assert env.client.get("/api/v1/projects/p-plain/git/pr-state").json() == {
        "current_branch": None, "has_github": False, "open_pr": None,
        "merged_head_sha": None, "pr_lookup_failed": False, "stale": False,
    }

    git(env.repo, "remote", "remove", "origin")
    data = env.client.get(f"{BASE}/pr-state").json()
    assert data["current_branch"] == "feat/x" and data["has_github"] is False
