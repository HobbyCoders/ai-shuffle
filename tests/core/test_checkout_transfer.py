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
    with pytest.raises(ValueError) as raised:
        export_checkout(source, "feature/only-on-worker")
    # Names what is unpublished on each side and how to resolve it.
    message = str(raised.value)
    assert f"Branch feature/only-on-worker in the owning worker's checkout {source} has 1 commit (newest: " in message
    assert "that is not on origin/feature/only-on-worker" in message
    assert "origin/feature/only-on-worker has 1 commit that it does not have" in message
    assert "Pull or rebase feature/only-on-worker" in message
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
    # Origin no longer contains the handed-off commit (force-moved elsewhere).
    git(source, "push", "--force", "origin", "main:refs/heads/feature/only-on-worker")
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


def _commit(path, name, content):
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(path, "add", name)
    git(path, "commit", "-m", f"add {name}")


PUBLIC_PEM = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----\n"
PRIVATE_PEM = "EXAMPLE-PEM-PRIVATE-KEY-FIXTURE\n"


def test_tracked_public_key_does_not_block_resume(tmp_path):
    from app.core.checkout_transfer import _preflight
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    _commit(repo, "ci/release-public-key.pem", PUBLIC_PEM)
    _commit(repo, "build/cert.pem", "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    _preflight(repo)


@pytest.mark.parametrize("name,content", [
    ("deploy/server.pem", PRIVATE_PEM),
    ("id_ed25519.key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----\n"),
    ("config/.env", "TOKEN=secret\n"),
])
def test_private_credentials_still_block_resume(tmp_path, name, content):
    from app.core.checkout_transfer import _preflight
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    _commit(repo, name, content)
    with pytest.raises(ValueError, match="Credential file"):
        _preflight(repo)


# ---------------------------------------------------------------------------
# Continue here must only refuse for work that exists nowhere on origin.
# ---------------------------------------------------------------------------

def _clone(tmp_path, name):
    path = tmp_path / name
    git(tmp_path, "clone", str(tmp_path / "remote.git"), str(path))
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def _head(path, ref="HEAD"):
    return git(path, "rev-parse", ref).decode().strip()


def _record_pushes(monkeypatch):
    import app.core.checkout_transfer as transfer
    pushes = []
    real = transfer.git

    def recording(cwd, *args):
        if args[0] == "push":
            pushes.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(transfer, "git", recording)
    return pushes


def test_clean_main_in_sync_with_origin_resumes_without_pushing(repositories, tmp_path, monkeypatch):
    """The reported case: a base-checkout session on a clean main that matches origin/main."""
    source, target = repositories
    git(source, "checkout", "main")
    other = _clone(tmp_path, "other")
    _commit(other, "merged.txt", "a merged pull request")
    git(other, "push", "origin", "main")
    git(source, "pull", "--ff-only", "origin", "main")
    pushes = _record_pushes(monkeypatch)
    state = export_checkout(source, "main", remote_identity(target))
    assert pushes == []
    assert state["head"] == _head(source) == _head(other)
    # The receiving base checkout is clean and behind: it fast-forwards.
    assert import_checkout(target, tmp_path / "unused", state) == str(target)
    assert fingerprint(target) == state["head"]


def test_clean_owner_behind_origin_has_nothing_to_publish(repositories, tmp_path, monkeypatch):
    """Origin moved on after the owner's last pull. Before the fix the push was
    rejected as non-fast-forward and the move refused, though nothing was unpublished."""
    source, target = repositories
    git(source, "checkout", "main")
    head = _head(source)
    other = _clone(tmp_path, "other")
    _commit(other, "later.txt", "pushed from elsewhere")
    git(other, "push", "origin", "main")
    pushes = _record_pushes(monkeypatch)
    state = export_checkout(source, "main")
    assert pushes == []
    assert state["head"] == head
    assert _head(source, "refs/remotes/origin/main") == _head(other)
    assert import_checkout(target, tmp_path / "unused", state) == str(target)
    assert fingerprint(target) == head


def test_pushed_branch_in_sync_with_upstream_resumes(repositories, tmp_path, monkeypatch):
    source, target = repositories
    git(source, "push", "-u", "origin", "feature/only-on-worker")
    destination = tmp_path / "resumed"
    first = export_checkout(source, "feature/only-on-worker")
    import_checkout(target, destination, first)
    pushes = _record_pushes(monkeypatch)
    again = export_checkout(source, "feature/only-on-worker")
    assert pushes == [] and again["head"] == first["head"]
    assert import_checkout(target, tmp_path / "other", again) == str(destination)
    assert fingerprint(destination) == again["head"]


@pytest.mark.parametrize("checked_out", [True, False])
def test_receiver_commits_already_published_upstream_do_not_block(repositories, tmp_path, checked_out):
    """The receiver's main is ahead of the owner's head, but only with commits
    origin already has. Before the fix: 'changed during resume' (checked out)
    or 'merge-base failed' (not checked out)."""
    source, target = repositories
    git(source, "checkout", "main")
    other = _clone(tmp_path, "other")
    _commit(other, "later.txt", "pushed from elsewhere")
    git(other, "push", "origin", "main")
    git(target, "pull", "--ff-only", "origin", "main")
    if not checked_out:
        git(target, "checkout", "-b", "elsewhere")
    state = export_checkout(source, "main")
    destination = tmp_path / "unused"
    cwd = import_checkout(target, destination, state)
    assert cwd == str(target if checked_out else destination)
    assert fingerprint(cwd) == state["head"]


@pytest.mark.parametrize("checked_out", [True, False])
def test_receiver_branch_squash_merged_upstream_does_not_block(repositories, tmp_path, checked_out):
    """The receiver committed on main; that work then reached origin/main
    through a squash merge, so its own commit is not an ancestor of origin."""
    source, target = repositories
    _commit(target, "fix.txt", "fixed on the receiver")
    other = _clone(tmp_path, "other")
    (other / "fix.txt").write_text("fixed on the receiver")
    (other / "review.txt").write_text("review follow-up")
    git(other, "add", ".")
    git(other, "commit", "-m", "Squash-merge the fix (#1)")
    git(other, "push", "origin", "main")
    git(source, "checkout", "main")
    git(source, "pull", "--ff-only", "origin", "main")
    if not checked_out:
        git(target, "checkout", "-b", "elsewhere")
    state = export_checkout(source, "main")
    destination = tmp_path / "unused"
    cwd = import_checkout(target, destination, state)
    assert cwd == str(target if checked_out else destination)
    assert fingerprint(cwd) == state["head"]
    assert git(target, "rev-parse", "refs/heads/main").decode().strip() == state["head"]


def test_receiver_rebased_branch_does_not_block(repositories, tmp_path):
    """Origin's branch was rebased and force-pushed; the receiver's stale copy
    holds only patch-equivalent commits and a stale remote-tracking ref."""
    source, target = repositories
    git(source, "push", "origin", "feature/only-on-worker")
    destination = tmp_path / "resumed"
    import_checkout(target, destination, export_checkout(source, "feature/only-on-worker"))
    git(source, "checkout", "main")
    _commit(source, "base-update.txt", "main moved")
    git(source, "push", "origin", "main")
    git(source, "checkout", "feature/only-on-worker")
    git(source, "rebase", "main")
    git(source, "push", "--force", "origin", "feature/only-on-worker")
    state = export_checkout(source, "feature/only-on-worker")
    assert import_checkout(target, tmp_path / "other", state) == str(destination)
    assert fingerprint(destination) == state["head"]


def test_receiver_uncommitted_change_refuses_and_names_it(repositories, tmp_path):
    source, target = repositories
    git(source, "checkout", "main")
    (source / "owner.txt").write_text("owner work")
    state = export_checkout(source, "main")
    (target / "file.txt").write_text("receiver edit")
    (target / "scratch.txt").write_text("receiver scratch")
    with pytest.raises(ValueError) as raised:
        import_checkout(target, tmp_path / "unused", state)
    message = str(raised.value)
    assert message.startswith("The receiving checkout has local changes:")
    assert f"the receiving worker's checkout {target} on branch main has 1 modified file and 1 untracked file" in message
    assert "file.txt" in message and "scratch.txt" in message
    assert "Commit and push, stash or discard them" in message
    assert (target / "file.txt").read_text() == "receiver edit"
    assert not (target / "owner.txt").exists()


@pytest.mark.parametrize("checked_out", [True, False])
def test_receiver_commit_ahead_of_upstream_refuses_and_names_it(repositories, tmp_path, checked_out):
    source, target = repositories
    _commit(target, "mine.txt", "only on the receiver")
    mine = _head(target)
    if not checked_out:
        git(target, "checkout", "-b", "elsewhere")
    git(source, "checkout", "main")
    (source / "owner.txt").write_text("owner work")
    state = export_checkout(source, "main")
    with pytest.raises(ValueError) as raised:
        import_checkout(target, tmp_path / "unused", state)
    message = str(raised.value)
    where = f"checkout {target}" if checked_out else f"repository {target}"
    assert f"Branch main in the receiving worker's {where} has 1 commit (newest: {mine[:7]}" in message
    assert "add mine.txt) that is not on origin" in message
    assert "Push them from that worker" in message
    assert _head(target, "refs/heads/main") == mine
    assert not (tmp_path / "unused").exists()
