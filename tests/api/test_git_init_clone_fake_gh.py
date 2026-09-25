"""Init / clone into the workspace, and option injection through push argv.

Real repositories under ``tmp_path`` and a stub ``gh`` on PATH for
``create_github_repo``. Pins docs/content/api-reference/miscellaneous.md
("POST .../git/init", "POST .../git/clone", "Other git operations": push)
including the state a failed step leaves behind.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.common import reset_served_roots_cache
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
    # Local clone sources are checked against the (briefly cached) served roots.
    reset_served_roots_cache()
    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    with TestClient(build_git_app()) as client:
        yield SimpleNamespace(client=client, gh=gh, tmp=tmp_path, workspace=workspace)
    reset_git_state()
    reset_served_roots_cache()


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


def _source_repo(env, parent=None):
    """A bare repository to clone from; by default inside the workspace, the
    only place (with project folders) an owner may clone a local path from."""
    parent = env.workspace / "sources" if parent is None else parent
    parent.mkdir(parents=True, exist_ok=True)
    src = init_repo(env.tmp / f"src-{parent.name}", {"hello.txt": "hi\n"})
    bare = parent / "src.git"
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
    assert sorted(p.name for p in env.workspace.iterdir()) == ["sources", "team"]


def test_clone_url_cannot_redirect_the_target(env):
    bare = _source_repo(env)
    database.create_project("p-clone", "p-clone", None, "here")

    # A path-looking suffix is part of the URL, not a destination argument.
    response = env.client.post("/api/v1/projects/p-clone/git/clone",
                               json={"clone_url": f"{bare} ../../escaped"})

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to clone repository:")
    assert not (env.tmp / "escaped").exists() and not (env.workspace.parent / "escaped").exists()
    assert not (env.workspace / "escaped").exists() and not (bare.parent / "escaped").exists()


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


@pytest.mark.parametrize("url", ["missing-repo.git", "https://127.0.0.1:9/r.git"])
def test_clone_bad_url_is_500(env, url):
    target = _project(env, "p-clone", "bad-url")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to clone repository:")
    assert not _is_repo(target)


@pytest.mark.parametrize("url, detail", [
    ("file::/tmp/x", "Refusing unsafe clone URL scheme"),
    ("ext::sh -c touch% x", "Refusing unsafe clone URL scheme"),
    ("fd::3", "Refusing unsafe clone URL scheme"),
    ("-oProxyCommand=touch x", "Invalid clone URL"),
    ("git://127.0.0.1:9/r.git", "Unsupported clone URL"),
    ("ftp://example.invalid/r.git", "Unsupported clone URL"),
    ("-host:repo", "Invalid clone URL"),
    ("file://localhost/tmp/r.git", "A file:// clone URL"),
])
def test_clone_unsupported_url_is_400(env, url, detail):
    target = _project(env, "p-clone", "bad-scheme")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 400
    assert response.json()["detail"].startswith(detail)
    assert not _is_repo(target)


# -- local clone sources (R9-3) ---------------------------------------------

OUTSIDE_DETAIL = "A local clone source must be inside the workspace or a project folder"


def _outside_repo(env):
    """A readable repository on the host outside every served root."""
    return _source_repo(env, env.tmp / "elsewhere")


@pytest.mark.parametrize("form", ["path", "file_url", "relative", "symlink"])
def test_owner_cannot_clone_a_local_repo_outside_the_served_roots(env, form):
    outside = _outside_repo(env)
    target = _project(env, "p-clone", "grab")
    if form == "symlink":
        link = env.workspace / "sources"
        link.mkdir(exist_ok=True)
        (link / "alias.git").symlink_to(outside)
        url = str(link / "alias.git")
    else:
        url = {
            "path": str(outside),
            "file_url": f"file://{outside}",
            "relative": f"../../elsewhere/{outside.name}",
        }[form]
    if form == "relative":
        # Relative sources resolve against the clone target, as git does.
        assert (target / url).resolve() == outside.resolve()

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == OUTSIDE_DETAIL
    assert not _is_repo(target) and not (target / "hello.txt").exists()


@pytest.mark.parametrize("form", ["path", "file_url"])
def test_owner_can_clone_a_local_repo_inside_the_workspace(env, form):
    bare = _source_repo(env)
    target = _project(env, "p-clone", "local-ok")
    url = str(bare) if form == "path" else f"file://{bare}"

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 200, response.text
    assert (target / "hello.txt").read_text() == "hi\n"


def test_owner_can_clone_another_registered_project(env, tmp_path):
    # A project folder outside the workspace is a served root too.
    other = _source_repo(env, tmp_path / "other-project")
    database.create_project("p-other", "p-other", None, str(other.parent))
    target = _project(env, "p-clone", "from-project")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(other)})

    assert response.status_code == 200, response.text
    assert (target / "hello.txt").read_text() == "hi\n"


@pytest.fixture
def as_api_key(monkeypatch):
    """Requests arrive as an API key scoped to ``p-clone``."""
    from app.api import common
    from app.api import git as git_api

    key = {"id": "key-1", "project_id": "p-clone"}
    monkeypatch.setattr(git_api, "get_api_user_from_request", lambda request: key)
    monkeypatch.setattr(common, "get_api_user_from_request", lambda request: key)
    return key


@pytest.mark.parametrize("form", ["path", "file_url", "relative"])
def test_api_key_cannot_clone_another_projects_repository(env, as_api_key, form):
    # The victim is a registered project inside the workspace: an owner could
    # clone it, a key scoped to p-clone must not.
    victim = _source_repo(env, env.workspace / "victim")
    database.create_project("p-victim", "p-victim", None, "victim")
    target = _project(env, "p-clone", "thief")
    url = {"path": str(victim), "file_url": f"file://{victim}", "relative": "../victim/src.git"}[form]

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "API keys can clone only HTTPS or SSH remotes"
    assert not _is_repo(target) and not (target / "hello.txt").exists()


@pytest.mark.parametrize("url", [
    "C:\\Users\\me\\other\\repo", "C:/Users/me/other/repo", "d:\\", "\\\\server\\share\\repo",
])
def test_api_key_cannot_clone_a_windows_path(env, as_api_key, url):
    # R10-9: on a Windows install git reads a drive-letter path as a local
    # path, not as scp-like ssh to host "C", so a key must not get it through.
    target = _project(env, "p-clone", "thief-win")

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "API keys can clone only HTTPS or SSH remotes"
    assert not _is_repo(target)


def test_api_key_can_clone_an_https_remote(env, as_api_key, monkeypatch):
    bare = _source_repo(env)
    target = _project(env, "p-clone", "keyed")
    url = "https://example.invalid/r.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 200, response.text
    assert (target / "hello.txt").read_text() == "hi\n"


def test_clone_response_never_echoes_url_credentials(env, monkeypatch):
    bare = _source_repo(env)
    target = _project(env, "p-clone", "with-creds")
    url = "https://alice:s3cr3t@example.invalid/r.git"
    # Resolve the credentialed URL to the local bare repo: a real clone, no network.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)

    response = env.client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Repository cloned from https://***@example.invalid/r.git"
    assert (target / "hello.txt").read_text() == "hi\n"


def test_clone_error_never_echoes_url_credentials(env):
    target = _project(env, "p-clone", "creds-fail")

    response = env.client.post(
        "/api/v1/projects/p-clone/git/clone", json={"clone_url": "http://alice:s3cr3t@127.0.0.1:9/r.git"},
    )

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to clone repository:")
    assert "s3cr3t" not in response.text
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

    monkeypatch.setattr(git_commit_ops, "_is_plain_branch_name", lambda branch: True)
    monkeypatch.setattr(git_commit_ops, "_push_refspec", lambda branch: branch)
    with pytest.raises(AssertionError):
        _assert_receive_pack_not_run(env)


def _push_refspec_branch(env, branch):
    repo = github_repo(env.tmp, name="refspecrepo")
    git(repo, "checkout", "-q", "main")
    git(repo, "push", "-q", "origin", "main:keep-me")
    database.create_project("p-refspec", "p-refspec", None, str(repo))
    response = env.client.post("/api/v1/projects/p-refspec/git/push", json={"branch": branch})
    return response, repo


def test_push_branch_refspec_cannot_delete_a_remote_branch(env):
    """R8-4: ``branch=":keep-me"`` reached ``git push origin :keep-me`` and
    deleted the remote branch."""
    response, repo = _push_refspec_branch(env, ":keep-me")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid branch name: ':keep-me'"
    assert git(bare_of(repo), "branch", "--list", "keep-me") != ""


def test_push_option_like_remote_is_400(env):
    repo = github_repo(env.tmp, name="remoterepo")
    database.create_project("p-push", "p-push", None, str(repo))

    response = env.client.post("/api/v1/projects/p-push/git/push", json={"remote": "--mirror"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid remote name: --mirror"
    assert not git(bare_of(repo), "branch", "--list", "feat/x")


@pytest.mark.parametrize("pid, url", [
    # A project id per case: the repo-record cache outlives the test database.
    ("p-clone-userpw", "https://alice:s3cr3t@example.invalid/r.git"),
    ("p-clone-token", "https://ghp_s3cr3t@example.invalid/r.git"),
])
def test_clone_stores_remote_without_url_credentials(env, monkeypatch, pid, url):
    bare = _source_repo(env)
    target = _project(env, pid, "stored-creds")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)
    # The remote exactly as the clone's .git/config holds it, whichever reader
    # (pygit2 or the CLI, which applies insteadOf) serves the route.
    from app.core.git_service import git_service
    monkeypatch.setattr(git_service, "get_remote_url",
                        lambda wd: git(wd, "config", "--get", "remote.origin.url"))

    response = env.client.post(f"/api/v1/projects/{pid}/git/clone", json={"clone_url": url})

    assert response.status_code == 200, response.text
    record = database.get_git_repository(response.json()["repository_id"])
    assert record["remote_url"] == "https://example.invalid/r.git"
    assert "s3cr3t" not in str(record)
    # git itself keeps the working, credentialed remote.
    assert git(target, "config", "--get", "remote.origin.url") == url
    git(target, "fetch", "-q", "origin")


# =============================================================================
# the clone token never reaches a client (R9-8)
# =============================================================================


@pytest.mark.parametrize("backend", ["native", "cli"])
def test_status_reports_the_remote_without_url_credentials(env, monkeypatch, backend):
    from app.core.git_native import HAS_PYGIT2
    from tests.git_repo_helpers import use_backend

    if backend == "native" and not HAS_PYGIT2:
        pytest.skip("pygit2 unavailable")
    use_backend(monkeypatch, backend)
    pid = f"p-status-creds-{backend}"
    target = _project(env, pid, f"status-creds-{backend}")
    init_repo(target)
    url = "https://ghp_s3cr3t@github.com/o/r.git"
    git(target, "remote", "add", "origin", url)

    response = env.client.get(f"/api/v1/projects/{pid}/git/status")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["remote_url"] == "https://github.com/o/r.git"
    assert data["github_repo_name"] == "o/r"
    assert "s3cr3t" not in response.text
    # The record the status poll created (and caches) holds no token either.
    record = database.get_git_repository(data["repository_id"])
    assert record["remote_url"] == "https://github.com/o/r.git"
    # git keeps the working, credentialed remote.
    assert git(target, "config", "--get", "remote.origin.url") == url


def test_repository_record_keeps_ssh_remotes_as_they_are(env):
    from app.api.git_helpers import ensure_git_repository_record

    target = _project(env, "p-record-ssh", "record-ssh")
    init_repo(target)
    git(target, "remote", "add", "origin", "git@github.com:o/r.git")

    record = ensure_git_repository_record("p-record-ssh", str(target))

    assert record["remote_url"] == "git@github.com:o/r.git"
    assert record["github_repo_name"] == "o/r"
