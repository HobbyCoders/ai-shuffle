import subprocess

import pytest
from app.core.checkout_transfer import export_checkout, import_checkout, fingerprint


def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path)


@pytest.fixture
def repositories(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "file.txt").write_text("original\n")
    (source / ".gitignore").write_text("ignored/\n.env\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    target = tmp_path / "target"
    git(tmp_path, "clone", str(source), str(target))
    git(source, "checkout", "-b", "feature/only-on-worker")
    (source / "commit.txt").write_text("unpushed commit")
    git(source, "add", ".")
    git(source, "commit", "-m", "unpublished work")
    return source, target


def test_round_trip_preserves_head_index_worktree_and_untracked(repositories, tmp_path):
    source, target = repositories
    (source / "file.txt").write_text("staged\n")
    git(source, "add", ".")
    (source / "file.txt").write_text("unstaged\n")
    (source / "binary.bin").write_bytes(bytes(range(256)))
    (source / ".env").write_text("DO_NOT_TRANSFER=secret")
    (source / "ignored").mkdir()
    (source / "ignored" / "dependency").write_text("not transferred")
    before = fingerprint(source)
    status = git(source, "status", "--porcelain", "-uall")
    state = export_checkout(source, "feature/only-on-worker")
    destination = tmp_path / "resumed"
    assert import_checkout(target, destination, state) == str(destination)
    assert fingerprint(source) == before == fingerprint(destination)
    assert import_checkout(target, destination, state) == str(destination)
    assert git(source, "status", "--porcelain", "-uall") == status
    assert git(destination, "status", "--porcelain", "-uall") == status
    assert git(source, "rev-parse", "HEAD") == git(destination, "rev-parse", "HEAD")
    assert not (destination / ".env").exists()
    assert not (destination / "ignored").exists()
    assert (destination / "binary.bin").read_bytes() == bytes(range(256))
    assert not git(source, "for-each-ref", "refs/shuffle-handoff")
    assert not git(target, "for-each-ref", "refs/shuffle-handoff")


def test_existing_local_work_is_untouched(repositories, tmp_path):
    source, target = repositories
    destination = tmp_path / "resumed"
    destination.mkdir()
    marker = destination / "local-work.txt"
    marker.write_text("keep me")
    state = export_checkout(source, "feature/only-on-worker")
    with pytest.raises(ValueError, match="already exists"):
        import_checkout(target, destination, state)
    assert marker.read_text() == "keep me"


def test_corrupted_bundle_does_not_create_checkout(repositories, tmp_path):
    source, target = repositories
    state = export_checkout(source, "feature/only-on-worker")
    state["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        import_checkout(target, tmp_path / "resumed", state)
    assert not (tmp_path / "resumed").exists()


def test_source_branch_must_match_session(repositories):
    source, _ = repositories
    with pytest.raises(ValueError, match="different branch"):
        export_checkout(source, "main")
