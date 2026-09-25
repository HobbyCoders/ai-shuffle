"""Init / clone into the workspace, and option injection through push argv.

Real repositories under ``tmp_path`` and a stub ``gh`` on PATH for
``create_github_repo``. Pins docs/content/api-reference/miscellaneous.md
("POST .../git/init", "POST .../git/clone", "Other git operations": push)
including the state a failed step leaves behind.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.db import database
from tests.gh_stub import FakeGh
from tests.git_flow_helpers import bare_of, build_git_app, github_repo, reset_git_state
from tests.git_repo_helpers import git, hermetic_git_env, init_repo, write

GH_NOT_SIGNED_IN = "GitHub CLI is not authenticated. Run 'gh auth login' in your terminal first."


@pytest.fixture
def env(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)
    reset_git_state()
    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    with TestClient(build_git_app()) as client:
        yield SimpleNamespace(client=client, gh=gh, tmp=tmp_path, workspace=workspace)
    reset_git_state()


def _project(env, pid, path, files=None):
    """Create a project row; relative *path* resolves under the workspace."""
    target = env.workspace / path
    if files is not None:
        target.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            write(target / rel, content)
    database.create_project(pid, pid, None, path)
    return target


def _is_repo(path):
    return (path / ".git").exists()


# =============================================================================
# init
# =============================================================================


def test_init_resolves_relative_project_under_the_workspace(env):
    target = _project(env, "p-init", "apps/new", {"index.js": "1\n"})

    response = env.client.post("/api/v1/projects/p-init/git/init", json={})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is True
    assert data["message"] == "Git repository initialized with initial commit"
    assert data["repository_id"] and data["github_repo_url"] is None and data["github_repo_name"] is None
    assert git(target, "log", "--format=%s") == "Initial commit"
    assert git(target, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # Nothing was initialised above the project folder.
    assert not _is_repo(env.workspace) and not _is_repo(env.workspace / "apps")


def test_init_without_initial_commit(env):
    target = _project(env, "p-init", "bare-init", {"a.txt": "a\n"})

    response = env.client.post("/api/v1/projects/p-init/git/init", json={"create_initial_commit": False})

    assert response.status_code == 200
    assert response.json()["message"] == "Git repository initialized"
    assert git(target, "rev-parse", "--verify", "HEAD", check=False) == ""
    assert git(target, "status", "--porcelain") == "?? a.txt"


def test_init_existing_repo_is_400(env):
    target = _project(env, "p-init", "has-repo")
    init_repo(target)
    head = git(target, "rev-parse", "HEAD")

    response = env.client.post("/api/v1/projects/p-init/git/init", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "Project is already a git repository"
    assert git(target, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("name", ["--mirror", "-q", "--force"])
def test_init_rejects_option_like_default_branch(env, name):
    """Regression: the pattern allowed a leading "-", creating a branch named
    e.g. ``--mirror`` that a later ``git push origin <branch>`` parsed as a flag."""
    target = _project(env, "p-init", "opt", {"a.txt": "a\n"})

    response = env.client.post("/api/v1/projects/p-init/git/init", json={"default_branch": name})

    assert response.status_code == 422
    assert not _is_repo(target)


def test_init_with_github_repo_creates_remote_and_pushes(env):
    target = _project(env, "p-init", "gh-app", {"a.txt": "a\n"})
    bare = env.tmp / "created.git"
    git(env.tmp, "init", "-q", "--bare", str(bare))
    env.gh.on("repo", "create", stdout={
        "name": "my-app", "nameWithOwner": "acme/my-app",
        "url": "https://github.com/acme/my-app", "sshUrl": str(bare),
    })

    response = env.client.post("/api/v1/projects/p-init/git/init", json={
        "create_github_repo": True, "github_repo_name": "my-app", "github_private": False,
        "default_branch": "trunk",
    })

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["message"] == "Git repository initialized with initial commit and linked to acme/my-app"
    assert data["github_repo_url"] == "https://github.com/acme/my-app"
    assert data["github_repo_name"] == "acme/my-app"
    assert env.gh.argvs("repo", "create") == [
        ["repo", "create", "my-app", "--public", "--json", "name,nameWithOwner,url,sshUrl"]
    ]
    assert git(target, "remote", "get-url", "origin") == str(bare)
    assert git(bare, "rev-parse", "trunk") == git(target, "rev-parse", "HEAD")


def test_init_github_needs_a_name_else_it_is_skipped(env):
    _project(env, "p-init", "no-name", {"a.txt": "a\n"})

    response = env.client.post("/api/v1/projects/p-init/git/init", json={"create_github_repo": True})

    assert response.status_code == 200
    assert response.json()["github_repo_url"] is None
    assert env.gh.calls("repo") == []


def test_init_github_not_signed_in_is_400_after_the_local_init(env):
    target = _project(env, "p-init", "signed-out", {"a.txt": "a\n"})
    env.gh.signed_in(False)

    response = env.client.post("/api/v1/projects/p-init/git/init", json={
        "create_github_repo": True, "github_repo_name": "my-app",
    })

    assert response.status_code == 400
    assert response.json()["detail"] == GH_NOT_SIGNED_IN
    # State left behind: the local repository and its initial commit exist,
    # without a remote, so a retry hits "already a git repository".
    assert git(target, "log", "--format=%s") == "Initial commit"
    assert git(target, "remote") == ""
    assert env.gh.calls("repo") == []


@pytest.mark.parametrize("name", ["--source=.", "--template=acme/evil", "-h"])
def test_init_option_like_github_repo_name_never_reaches_gh(env, name):
    """Regression: the name is a positional gh argument; "--source=." made
    ``gh repo create`` treat it as a flag."""
    _project(env, "p-init", "opt-name", {"a.txt": "a\n"})

    response = env.client.post("/api/v1/projects/p-init/git/init", json={
        "create_github_repo": True, "github_repo_name": name,
    })

    assert response.status_code == 500
    assert response.json()["detail"] == (
        f"Failed to create GitHub repository '{name}'. Check logs for details."
    )
    assert env.gh.calls("repo") == []


def test_init_github_create_failure_is_500(env):
    target = _project(env, "p-init", "gh-fails", {"a.txt": "a\n"})
    env.gh.on("repo", "create", code=1, stderr="Name already exists on this account")

    response = env.client.post("/api/v1/projects/p-init/git/init", json={
        "create_github_repo": True, "github_repo_name": "taken",
    })

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to create GitHub repository 'taken'. Check logs for details."
    assert git(target, "remote") == ""


# =============================================================================
# clone
# =============================================================================


def _source_repo(env):
    src = init_repo(env.tmp / "src", {"hello.txt": "hi\n"})
    bare = env.tmp / "src.git"
    git(env.tmp, "clone", "-q", "--bare", str(src), str(bare))
    return bare


def test_clone_into_missing_workspace_folder(env):
    bare = _source_repo(env)
    database.create_project("p-clone", "p-clone", None, "team/cloned")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(bare)})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data == {"success": True, "message": f"Repository cloned from {bare}",
                    "repository_id": data["repository_id"]}
    assert data["repository_id"]
    target = env.workspace / "team" / "cloned"
    assert (target / "hello.txt").read_text() == "hi\n"
    # The URL chooses what is cloned, never where: nothing lands elsewhere.
    assert sorted(p.name for p in env.workspace.iterdir()) == ["team"]


def test_clone_url_cannot_redirect_the_target(env):
    bare = _source_repo(env)
    database.create_project("p-clone", "p-clone", None, "here")

    # A path-looking suffix is part of the URL, not a destination argument.
    response = env.client.post("/api/v1/projects/p-clone/git/clone",
                               json={"clone_url": f"{bare} ../../escaped"})

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to clone repository:")
    assert not (env.tmp / "escaped").exists() and not (env.workspace.parent / "escaped").exists()


def test_clone_into_non_empty_folder_is_500_and_keeps_files(env):
    bare = _source_repo(env)
    target = _project(env, "p-clone", "occupied", {"mine.txt": "keep me\n"})

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(bare)})

    assert response.status_code == 500
    assert "already exists and is not an empty directory" in response.json()["detail"]
    assert (target / "mine.txt").read_text() == "keep me\n"
    assert not _is_repo(target)


def test_clone_into_existing_repo_is_400(env):
    bare = _source_repo(env)
    target = _project(env, "p-clone", "repo-here")
    init_repo(target)

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(bare)})

    assert response.status_code == 400
    assert response.json()["detail"] == "Project is already a git repository"


@pytest.mark.parametrize("url", ["/definitely/not/a/repo.git", "file::/tmp/x", "-oProxyCommand=touch x"])
def test_clone_bad_url_is_500(env, url):
    target = _project(env, "p-clone", "bad-url")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to clone repository:")
    assert not _is_repo(target)


@pytest.mark.parametrize("body", [{}, {"clone_url": ""}, {"clone_url": "x" * 501}])
def test_clone_url_validation_is_422(env, body):
    _project(env, "p-clone", "v")
    assert env.client.post("/api/v1/projects/p-clone/git/clone", json=body).status_code == 422


# =============================================================================
# push: branch / remote never parse as git options
# =============================================================================


def _push_with_receive_pack_branch(env):
    repo = github_repo(env.tmp, name="pushrepo")
    # On a branch with an upstream, a flag-only `git push origin <flag>`
    # proceeds to connect to the (local, file-transport) remote.
    git(repo, "checkout", "-q", "main")
    database.create_project("p-push", "p-push", None, str(repo))
    marker = env.tmp / "receive-pack-ran"
    response = env.client.post("/api/v1/projects/p-push/git/push", json={
        "branch": f"--receive-pack=touch {marker}; git-receive-pack",
    })
    return response, marker, repo


def _assert_receive_pack_not_run(env):
    response, marker, _ = _push_with_receive_pack_branch(env)
    assert not marker.exists(), "caller-supplied --receive-pack command was executed"
    assert response.status_code == 400


def test_push_branch_cannot_inject_receive_pack(env):
    """Regression: ``body.branch`` went to ``git push origin <branch>``
    verbatim, so ``--receive-pack=<cmd>`` ran <cmd> for a local/ssh remote."""
    _assert_receive_pack_not_run(env)


def test_push_branch_guard_bites_when_disabled(env, monkeypatch):
    from app.core import git_commit_ops

    monkeypatch.setattr(git_commit_ops, "_push_branch_arg", lambda branch: branch)
    with pytest.raises(AssertionError):
        _assert_receive_pack_not_run(env)


def test_push_option_like_remote_is_400(env):
    repo = github_repo(env.tmp, name="remoterepo")
    database.create_project("p-push", "p-push", None, str(repo))

    response = env.client.post("/api/v1/projects/p-push/git/push", json={"remote": "--mirror"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid remote name: --mirror"
    assert not git(bare_of(repo), "branch", "--list", "feat/x")
