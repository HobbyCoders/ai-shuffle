import subprocess

import pytest
from app.core.checkout_transfer import export_checkout, import_checkout, fingerprint, remote_identity, _remote_key


def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path)


@pytest.fixture
def repositories(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "checkout", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "file.txt").write_text("original\n")
    (source / ".gitignore").write_text("ignored/\n.env\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    target = tmp_path / "target"
    git(tmp_path, "clone", str(remote), str(target))
    git(target, "config", "user.name", "Test")
    git(target, "config", "user.email", "test@example.com")
    git(source, "checkout", "-b", "feature/only-on-worker")
    (source / "commit.txt").write_text("unpushed commit")
    git(source, "add", ".")
    git(source, "commit", "-m", "unpublished work")
    return source, target


def test_round_trip_commits_pushes_and_fetches_all_nonignored_work(repositories, tmp_path):
    source, target = repositories
    (source / "file.txt").write_text("staged\n")
    git(source, "add", ".")
    (source / "file.txt").write_text("unstaged\n")
    (source / "binary.bin").write_bytes(bytes(range(256)))
    (source / "commit.txt").unlink()
    (source / ".env").write_text("DO_NOT_TRANSFER=secret")
    (source / "ignored").mkdir()
    (source / "ignored" / "dependency").write_text("not transferred")
    state = export_checkout(source, "feature/only-on-worker", remote_identity(target))
    assert "bundle" not in state
    assert len(str(state)) < 1000
    destination = tmp_path / "resumed"
    assert import_checkout(target, destination, state) == str(destination)
    assert fingerprint(source) == fingerprint(destination) == state["head"]
    assert git(destination, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == b"origin/feature/only-on-worker"
    assert import_checkout(target, destination, state) == str(destination)
    assert not git(source, "status", "--porcelain", "-uall")
    assert not git(destination, "status", "--porcelain", "-uall")
    assert (destination / "file.txt").read_text() == "unstaged\n"
    assert not (destination / "commit.txt").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "ignored").exists()
    assert (destination / "binary.bin").read_bytes() == bytes(range(256))
    assert git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").decode().split()[0] == state["head"]


def test_existing_unregistered_destination_is_untouched(repositories, tmp_path):
    source, target = repositories
    destination = tmp_path / "resumed"
    destination.mkdir()
    marker = destination / "local-work.txt"
    marker.write_text("keep me")
    state = export_checkout(source, "feature/only-on-worker")
    with pytest.raises(ValueError, match="already exists"):
        import_checkout(target, destination, state)
    assert marker.read_text() == "keep me"


def test_clean_existing_branch_fast_forwards_in_its_registered_worktree(repositories, tmp_path):
    source, target = repositories
    first = export_checkout(source, "feature/only-on-worker")
    destination = tmp_path / "existing"
    import_checkout(target, destination, first)
    (source / "new.txt").write_text("next change")
    second = export_checkout(source, "feature/only-on-worker")
    assert import_checkout(target, tmp_path / "different", second) == str(destination)
    assert fingerprint(destination) == second["head"]
    assert (destination / "new.txt").read_text() == "next change"


@pytest.mark.parametrize("kind", ["dirty", "ahead", "divergent"])
def test_local_work_is_never_overwritten(repositories, tmp_path, kind):
    source, target = repositories
    state = export_checkout(source, "feature/only-on-worker")
    destination = tmp_path / "resumed"
    import_checkout(target, destination, state)
    (destination / "mine.txt").write_text("keep mine")
    if kind != "dirty":
        git(destination, "add", ".")
        git(destination, "commit", "-m", "local work")
    if kind == "divergent":
        (source / "theirs.txt").write_text("remote work")
        state = export_checkout(source, "feature/only-on-worker")
    before = git(destination, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        import_checkout(target, destination, state)
    assert (destination / "mine.txt").read_text() == "keep mine"
    assert git(destination, "rev-parse", "HEAD") == before


def test_remote_mismatch_refuses_before_commit(repositories):
    source, target = repositories
    (source / "new.txt").write_text("private work")
    before = git(source, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="different repositories"):
        export_checkout(source, "feature/only-on-worker", "wrong")
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "status", "--porcelain")


def test_push_rejection_keeps_local_commit_without_force(repositories):
    source, target = repositories
    state = export_checkout(source, "feature/only-on-worker")
    git(target, "fetch", "origin")
    git(target, "checkout", "-b", "feature/only-on-worker", "origin/feature/only-on-worker")
    (target / "theirs").write_text("other commit")
    git(target, "add", ".")
    git(target, "commit", "-m", "other commit")
    git(target, "push", "origin", "HEAD")
    remote_head = git(target, "rev-parse", "HEAD").decode().strip()
    (source / "mine").write_text("my change")
    with pytest.raises(ValueError, match="push failed"):
        export_checkout(source, "feature/only-on-worker")
    assert fingerprint(source) != state["head"]
    assert git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").decode().split()[0] == remote_head


def test_no_origin_and_unignored_credentials_are_rejected(repositories):
    source, _ = repositories
    (source / "secret.key").write_text("private")
    before = git(source, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Credential"):
        export_checkout(source, "feature/only-on-worker")
    assert git(source, "rev-parse", "HEAD") == before
    git(source, "remote", "remove", "origin")
    with pytest.raises(ValueError):
        export_checkout(source, "feature/only-on-worker")


def test_ssh_https_remote_identity_does_not_include_credentials():
    assert _remote_key("git@github.com:Owner/repo.git") == _remote_key("https://token@github.com/owner/repo.git")
    assert _remote_key("https://git.example:8443/a.git") != _remote_key("https://git.example/a.git")


def test_old_bundle_protocol_and_changed_remote_branch_rejected(repositories, tmp_path):
    source, target = repositories
    state = export_checkout(source, "feature/only-on-worker")
    with pytest.raises(ValueError, match="Update both"):
        import_checkout(target, tmp_path / "resumed", {**state, "protocol": "old"})
    (source / "later").write_text("later")
    export_checkout(source, "feature/only-on-worker")
    with pytest.raises(ValueError, match="remote branch changed"):
        import_checkout(target, tmp_path / "resumed", state)
    assert not (tmp_path / "resumed").exists()


def test_existing_base_branch_is_reused(repositories, tmp_path):
    source, target = repositories
    git(source, "checkout", "main")
    (source / "new-main.txt").write_text("updated main")
    state = export_checkout(source, "main")
    assert import_checkout(target, tmp_path / "unused", state) == str(target)
    assert fingerprint(target) == state["head"]


def test_fast_forward_preserves_ignored_local_files(repositories, tmp_path):
    source, target = repositories
    state = export_checkout(source, "feature/only-on-worker")
    destination = tmp_path / "resumed"
    import_checkout(target, destination, state)
    (destination / "ignored").mkdir()
    (destination / "ignored" / "mine").write_text("local ignored content")
    (source / "ignored").mkdir()
    (source / "ignored" / "mine").write_text("now tracked remotely")
    git(source, "add", "-f", "ignored/mine")
    state = export_checkout(source, "feature/only-on-worker")
    with pytest.raises(ValueError, match="merge failed"):
        import_checkout(target, destination, state)
    assert (destination / "ignored" / "mine").read_text() == "local ignored content"
