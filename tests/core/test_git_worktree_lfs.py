"""Real-Git regression coverage for unavailable LFS during worktree creation."""
import hashlib
import os
import shlex
import subprocess
import sys

import pytest

from app.core.git_service import GitService
from fluid.context.runtime_facts import runtime_facts

POINTER = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 12\n"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10).stdout.strip()


@pytest.fixture
def lfs_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    (repo / "asset.bin").write_text(POINTER)
    git(repo, "-c", "filter.lfs.required=false", "-c", "filter.lfs.clean=", "add", ".")
    git(repo, "commit", "-m", "LFS pointer")
    binary = tmp_path / "missing-tooling" / "git-lfs"
    for key, value in (("process", f"{binary} filter-process"),
                       ("smudge", f"{binary} smudge -- %f"),
                       ("clean", f"{binary} clean -- %f"), ("required", "true")):
        git(repo, "config", f"filter.lfs.{key}", value)
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 2\n")
    hook.chmod(0o755)
    return repo


@pytest.mark.parametrize("new_branch", [True, False])
def test_missing_lfs_retries_once_with_pointer_files(lfs_repo, tmp_path, caplog, monkeypatch, new_branch):
    repo, dest = lfs_repo, tmp_path / "worktree"
    if not new_branch:
        git(repo, "branch", "feature")
    config = (repo / ".git" / "config").read_bytes()
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook_contents, hook_mode = hook.read_bytes(), hook.stat().st_mode
    service = GitService()
    run = service._run_git
    adds = []

    def traced(cwd, args, **kwargs):
        result = run(cwd, args, **kwargs)
        if "worktree" in args and "add" in args:
            adds.append((list(args), result.returncode))
            if len(adds) == 1:
                # Real filter failures roll back both files and registration.
                assert result.returncode != 0 and not dest.exists()
                assert str(dest) not in git(repo, "worktree", "list", "--porcelain")
                if new_branch:
                    # The failed -b still creates its ref; retry must reuse it.
                    assert git(repo, "rev-parse", "feature") == git(repo, "rev-parse", "main")
        return result

    monkeypatch.setattr(service, "_run_git", traced)
    success, warning = service.add_worktree(str(repo), str(dest), "feature", new_branch=new_branch)
    assert success, warning
    assert len(adds) == 2 and adds[0][1] != 0 and adds[1][1] == 0
    assert "-b" not in adds[1][0]
    for setting in ("filter.lfs.required=false", "filter.lfs.process=", "filter.lfs.smudge=", "filter.lfs.clean="):
        assert setting in adds[1][0]
    assert any(arg.startswith("core.hooksPath=") for arg in adds[1][0])
    assert (dest / "asset.bin").read_text() == POINTER
    assert "placeholders" in warning and "git lfs pull" in warning
    assert warning in caplog.text
    assert warning in runtime_facts(str(dest))
    from fluid.context.environment import environment_note
    assert warning in environment_note(str(dest))
    assert (repo / ".git" / "config").read_bytes() == config
    assert hook.read_bytes() == hook_contents and hook.stat().st_mode == hook_mode
    assert git(dest, "config", "filter.lfs.required") == "true"
    assert git(dest, "symbolic-ref", "--short", "HEAD") == "feature"


def test_invalid_branch_does_not_retry(lfs_repo, tmp_path, monkeypatch):
    service = GitService()
    run, adds = service._run_git, []

    def traced(cwd, args, **kwargs):
        if "worktree" in args and "add" in args:
            adds.append(args)
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(service, "_run_git", traced)
    success, error = service.add_worktree(str(lfs_repo), str(tmp_path / "bad"), "bad..branch", new_branch=True)
    assert not success and "not a valid branch name" in error
    assert len(adds) == 1


@pytest.mark.parametrize("diagnostic, expected", [
    ("git-lfs: not found", True),
    ("error: external filter 'git-lfs filter-process' failed", True),
    ("fatal: asset.bin: smudge filter lfs failed", True),
    ("filter.lfs.process failed with exit status 2", True),
    ("git-lfs: Permission denied", True),
    ("git-lfs: Exec format error", True),
    ("git-lfs is not recognized as an internal or external command", True),
    ("fatal: the remote end hung up unexpectedly", False),
    ("some-driver filter-process: not found", False),
    ("fatal: invalid reference: git-lfs", False),
    ("fatal: invalid reference: git-lfs-failed", False),
    ("fatal: 'git-lfs-failed' is not a valid branch name", False),
    ("Preparing worktree (new branch 'git-lfs-failed')\nfatal: invalid reference: missing", False),
    ("fatal: permission denied", False),
])
def test_lfs_failure_detector(diagnostic, expected):
    from app.core.git_worktree_ops import _is_lfs_failure
    assert _is_lfs_failure(diagnostic) is expected


def test_lfs_hook_failure_keeps_registered_worktree(checkout_project, lfs_repo, monkeypatch):
    from app.core import checkout_manager

    repo = lfs_repo
    dest = checkout_manager._branch_checkout_path(str(repo), "hook-branch")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Checkout itself succeeds; only the unavailable-LFS hook fails.
    git(repo, "config", "filter.lfs.process", "")
    git(repo, "config", "filter.lfs.smudge", "")
    git(repo, "config", "filter.lfs.required", "false")
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\necho 'git-lfs: not found' >&2\nexit 2\n")
    service, first, mutations = checkout_manager.git_service, [], []
    run = service._run_git

    def traced(cwd, args, **kwargs):
        result = run(cwd, args, **kwargs)
        if args[:2] in (["worktree", "add"], ["worktree", "remove"], ["worktree", "prune"]):
            mutations.append(args[:2])
        if "worktree" in args and "add" in args and not first:
            assert result.returncode == 2
            assert dest.is_dir() and (dest / ".git").is_file()
            assert str(dest) in git(repo, "worktree", "list", "--porcelain")
            # Interleave the real adoption path before the first caller handles
            # Git's failed hook. A second session can now write into this cwd.
            adopted = checkout_manager.ensure_checkout(checkout_project, "hook-branch", provision_runtime=False)
            assert adopted == str(dest)
            (dest / "agent-work.txt").write_text("work written after adoption")
            (dest / "asset.bin").write_text("agent edit after adoption\n")
            first.append(result)
        return result

    monkeypatch.setattr(service, "_run_git", traced)
    success, warning = service.add_worktree(str(repo), str(dest), "hook-branch", new_branch=True)
    assert success, warning
    assert first and "placeholders" in warning
    assert (dest / "agent-work.txt").read_text() == "work written after adoption"
    assert (dest / "asset.bin").read_text() == "agent edit after adoption\n"
    assert mutations == [["worktree", "add"]]  # No retry, remove, or prune.
    assert warning in runtime_facts(str(dest))
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert "git-lfs: not found" in hook.read_text()


@pytest.mark.parametrize("damage", ["missing-file", "missing-index", "staged-deletion"])
def test_registered_partial_checkout_fails_closed(lfs_repo, tmp_path, monkeypatch, damage):
    from pathlib import Path
    from fluid.context.runtime_facts import lfs_fallback_warning

    repo, dest = lfs_repo, tmp_path / "registered-partial"
    git(repo, "config", "filter.lfs.process", "")
    git(repo, "config", "filter.lfs.smudge", "")
    git(repo, "config", "filter.lfs.required", "false")
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\necho 'git-lfs: not found' >&2\nexit 2\n")
    service, mutations = GitService(), []
    run = service._run_git

    def traced(cwd, args, **kwargs):
        result = run(cwd, args, **kwargs)
        if args[:2] in (["worktree", "add"], ["worktree", "remove"], ["worktree", "prune"]):
            mutations.append(args[:2])
        if args[:2] == ["worktree", "add"]:
            assert result.returncode == 2
            assert str(dest) in git(repo, "worktree", "list", "--porcelain")
            # A real registered checkout with incomplete files/index simulates
            # the state left when an OS lock prevents Git's checkout rollback.
            if damage == "missing-index":
                index = Path(git(dest, "rev-parse", "--git-path", "index"))
                (index if index.is_absolute() else dest / index).unlink()
            elif damage == "staged-deletion":
                git(dest, "rm", "--cached", "--", "asset.bin")
            else:
                (dest / "asset.bin").unlink()
            (dest / "agent-work.txt").write_text("must survive failed recovery")
        return result

    monkeypatch.setattr(service, "_run_git", traced)
    success, error = service.add_worktree(str(repo), str(dest), "partial", new_branch=True)
    assert not success and "git-lfs: not found" in error
    assert git(dest, "symbolic-ref", "--short", "HEAD") == "partial"
    assert (dest / "agent-work.txt").read_text() == "must survive failed recovery"
    assert str(dest) in git(repo, "worktree", "list", "--porcelain")
    assert mutations == [["worktree", "add"]]
    assert lfs_fallback_warning(str(dest)) is None


def test_unrelated_hook_failure_does_not_retry(lfs_repo, tmp_path, monkeypatch):
    git(lfs_repo, "config", "filter.lfs.process", "")
    git(lfs_repo, "config", "filter.lfs.smudge", "")
    git(lfs_repo, "config", "filter.lfs.required", "false")
    service, adds = GitService(), []
    run = service._run_git

    def traced(cwd, args, **kwargs):
        if "worktree" in args and "add" in args:
            adds.append(args)
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(service, "_run_git", traced)
    success, _ = service.add_worktree(str(lfs_repo), str(tmp_path / "wt"), "hook", new_branch=True)
    assert not success and len(adds) == 1


def break_other_filter(repo, tmp_path):
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n*.txt filter=other\n")
    (repo / "other.txt").write_text("other filter\n")
    git(repo, "-c", "filter.lfs.required=false", "-c", "filter.lfs.process=",
        "-c", "filter.lfs.clean=", "add", ".")
    git(repo, "-c", "filter.lfs.required=false", "-c", "filter.lfs.process=",
        "-c", "filter.lfs.clean=", "commit", "-m", "Second filter")
    git(repo, "config", "filter.other.process", f"{tmp_path}/missing-other filter-process")
    git(repo, "config", "filter.other.required", "true")


def test_failed_fallback_does_not_retry_again(lfs_repo, tmp_path, monkeypatch):
    break_other_filter(lfs_repo, tmp_path)
    service, adds = GitService(), []
    run = service._run_git

    def traced(cwd, args, **kwargs):
        if "worktree" in args and "add" in args:
            adds.append(args)
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(service, "_run_git", traced)
    success, error = service.add_worktree(str(lfs_repo), str(tmp_path / "wt"), "both", new_branch=True)
    assert not success and len(adds) == 2
    assert "Retry without LFS failed" in error and "missing-other" in error


def test_preexisting_files_are_not_cleaned_up(lfs_repo, tmp_path):
    dest = tmp_path / "existing"
    dest.mkdir()
    (dest / "keep").write_text("owner data")
    success, _ = GitService().add_worktree(str(lfs_repo), str(dest), "feature", new_branch=True)
    assert not success
    assert (dest / "keep").read_text() == "owner data"


@pytest.fixture
def checkout_project(mock_db, lfs_repo):
    from app.core.config import settings
    from app.db import checkouts, projects_v2
    project = projects_v2.upsert_project("git:lfs-regression", name="LFS regression")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(lfs_repo))
    return project


def test_checkout_manager_and_executor_preserve_git_error(checkout_project, lfs_repo, tmp_path):
    from app.core import checkout_manager, git_ops_runner, project_resolver
    from app.core.git_worktree_ops import WorktreeAddError
    from app.core.config import settings
    from app.db import git_ops
    break_other_filter(lfs_repo, tmp_path)
    op = git_ops.insert_op(settings.effective_instance_id, "ensure_worktree",
                           project_v2_id=checkout_project["id"],
                           params={"branch": "broken", "base_branch": "main", "create_new_branch": True})
    with pytest.raises(WorktreeAddError, match="missing-other") as error:
        git_ops_runner.execute_ensure_worktree(op)
    assert "exists neither locally" not in str(error.value)
    assert "Git worktree add failed" in str(error.value)
    # Legacy callers retain None; session callers explicitly opt into details.
    assert checkout_manager.ensure_checkout(checkout_project, "broken", provision_runtime=False) is None
    with pytest.raises(WorktreeAddError, match="missing-other"):
        checkout_manager.ensure_checkout(checkout_project, "broken", provision_runtime=False, raise_add_errors=True)
    session = {"id": "broken-session", "project_v2_id": checkout_project["id"], "branch": "broken"}
    assert project_resolver.resolve_cwd(session) is None
    with pytest.raises(WorktreeAddError, match="missing-other"):
        project_resolver.resolve_cwd(session, raise_add_errors=True)


@pytest.mark.asyncio
async def test_inline_error_reaches_session_message(checkout_project, lfs_repo, tmp_path, monkeypatch):
    from app.core import query_options as qo
    from app.core.config import settings
    from app.db import git_ops
    from tests.core.helpers import query_profile
    break_other_filter(lfs_repo, tmp_path)
    op = git_ops.insert_op(settings.effective_instance_id, "ensure_worktree",
                           project_v2_id=checkout_project["id"],
                           params={"branch": "inline-broken", "base_branch": "main", "create_new_branch": True})
    session = {"id": "inline-session", "project_v2_id": checkout_project["id"], "branch": "inline-broken"}
    assert await qo.ensure_pending_checkout(session) is False
    assert "missing-other" in git_ops.get_op(op["id"])["result"]["error"]
    monkeypatch.setattr(qo.database, "get_session", lambda _id: session)
    monkeypatch.setattr(qo, "get_security_instructions", lambda *args, **kwargs: "")
    with pytest.raises(qo.WorktreeUnavailableError) as error:
        qo.build_options_from_profile(profile=query_profile(), session_id=session["id"])
    message = str(error.value)
    assert "missing-other" in message and "could not be prepared" in message
    assert "no longer exists" not in message and "exists neither locally" not in message


def test_last_failed_claim_is_visible_if_no_branch_remains(checkout_project):
    from app.core import query_options as qo
    from app.core.config import settings
    from app.db import git_ops
    op = git_ops.insert_op(settings.effective_instance_id, "ensure_worktree",
                           project_v2_id=checkout_project["id"], params={"branch": "gone"})
    git_ops.fail_op(op["id"], "Git worktree add failed: permission denied")
    error = qo._last_checkout_error(checkout_project["id"], "gone")
    assert "permission denied" in error
    assert qo._last_checkout_error(checkout_project["id"], "unrelated") is None
    message = str(qo.WorktreeUnavailableError("session", "gone", checkout_error=error))
    assert "permission denied" in message and "no longer exists" not in message


def test_error_details_are_bounded_and_redacted():
    from app.core.git_worktree_ops import worktree_error_detail
    detail = worktree_error_detail("prefix" * 1000 + "fatal: https://user:private-value@example.test/repo: failed")
    assert len(detail) <= 2000 and "private-value" not in detail and "fatal:" in detail


@pytest.mark.parametrize("existing_branch", [True, False])
def test_checkout_transfer_uses_same_lfs_fallback(lfs_repo, tmp_path, existing_branch):
    from app.core.checkout_transfer import import_checkout, remote_identity, fingerprint
    from fluid.context.runtime_facts import lfs_fallback_warning

    origin = tmp_path / "origin.git"
    git(lfs_repo, "branch", "transfer")
    git(tmp_path, "clone", "--bare", str(lfs_repo), str(origin))
    git(lfs_repo, "remote", "add", "origin", str(origin))
    head = git(lfs_repo, "rev-parse", "transfer")
    if not existing_branch:
        # Only our fixture ref: receiver recovers the branch from origin.
        git(lfs_repo, "branch", "-d", "transfer")
    dest = tmp_path / "received"
    state = {"protocol": "git-origin-v1", "branch": "transfer", "head": head,
             "remote": remote_identity(lfs_repo)}
    assert import_checkout(lfs_repo, dest, state) == str(dest)
    assert (dest / "asset.bin").read_text() == POINTER
    assert lfs_fallback_warning(str(dest))
    # The handoff caller verifies again with no import-specific flags.
    assert fingerprint(dest) == head
    (dest / "asset.bin").write_text("agent changed the LFS file\n")
    with pytest.raises(ValueError, match="files changed during resume"):
        fingerprint(dest)
    (dest / "asset.bin").write_text(POINTER)
    (dest / "untracked.txt").write_text("agent work\n")
    with pytest.raises(ValueError, match="files changed during resume"):
        fingerprint(dest)


@pytest.mark.parametrize("driver, receipt", [("lfs", False), ("other", True)])
def test_status_override_requires_lfs_error_and_receipt(lfs_repo, tmp_path, monkeypatch, driver, receipt):
    from app.core import checkout_transfer as transfer
    from fluid.context.runtime_facts import record_lfs_fallback

    if driver == "other":
        (lfs_repo / ".gitattributes").write_text("*.bin filter=other\n")
        git(lfs_repo, "config", "filter.other.process", str(tmp_path / "missing-other-filter"))
        git(lfs_repo, "config", "filter.other.required", "true")
    if receipt:
        record_lfs_fallback(str(lfs_repo))
    asset = lfs_repo / "asset.bin"
    stat = asset.stat()
    os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    run, commands = transfer._run, []

    def traced(cwd, *args, **kwargs):
        commands.append(args)
        return run(cwd, *args, **kwargs)

    monkeypatch.setattr(transfer, "_run", traced)
    with pytest.raises(ValueError, match="Git status failed during resume"):
        transfer.fingerprint(lfs_repo)
    assert len(commands) == 1 and commands[0][0] == "status"


def test_repaired_lfs_filter_is_used_despite_fallback_receipt(lfs_repo, tmp_path):
    from app.core.checkout_transfer import fingerprint
    from app.core.git_worktree_ops import LFS_PASSTHROUGH_CONFIG
    from fluid.context.runtime_facts import record_lfs_fallback

    repo, asset = lfs_repo, lfs_repo / "asset.bin"
    hydrated = b"hydrated LFS asset contents\n"
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               f"oid sha256:{hashlib.sha256(hydrated).hexdigest()}\nsize {len(hydrated)}\n")
    asset.write_text(pointer)
    git(repo, *LFS_PASSTHROUGH_CONFIG, "add", "asset.bin")
    git(repo, *LFS_PASSTHROUGH_CONFIG, "commit", "-m", "Real asset pointer")
    record_lfs_fallback(str(repo))
    # A real Git clean driver implements LFS's content-to-pointer comparison.
    clean = tmp_path / "lfs_clean.py"
    clean.write_text(
        "import hashlib, sys\ndata = sys.stdin.buffer.read()\n"
        "if data.startswith(b'version https://git-lfs.github.com/spec/v1\\n'):\n"
        "    sys.stdout.buffer.write(data)\n"
        "else:\n"
        "    print('version https://git-lfs.github.com/spec/v1')\n"
        "    print('oid sha256:' + hashlib.sha256(data).hexdigest())\n"
        "    print('size ' + str(len(data)))\n"
    )
    git(repo, "config", "--unset", "filter.lfs.process")
    git(repo, "config", "filter.lfs.smudge", "")
    git(repo, "config", "filter.lfs.clean", f"{shlex.quote(sys.executable)} {shlex.quote(str(clean))}")
    asset.write_bytes(hydrated)
    assert git(repo, "show", "HEAD:asset.bin") == pointer.strip()
    assert git(repo, "hash-object", "--path=asset.bin", "asset.bin") == git(repo, "rev-parse", "HEAD:asset.bin")
    # Hydration also refreshes the index stat; the clean blob remains HEAD's pointer.
    git(repo, "add", "asset.bin")
    git(repo, "diff", "--cached", "--quiet")
    assert git(repo, "status", "--porcelain") == ""

    def touch():
        stat = asset.stat()
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    touch()
    assert git(repo, "status", "--porcelain") == ""  # Clean after real clean filtering.
    touch()  # Invalidate the refreshed index stat without changing any bytes.
    assert fingerprint(repo) == git(repo, "rev-parse", "HEAD")
    asset.write_bytes(hydrated + b"a genuine edit\n")
    with pytest.raises(ValueError, match="files changed during resume"):
        fingerprint(repo)


def test_unregistered_partial_directory_is_cleaned(lfs_repo, tmp_path, monkeypatch):
    dest = tmp_path / "partial"
    service = GitService()
    run, adds = service._run_git, []

    def traced(cwd, args, **kwargs):
        result = run(cwd, args, **kwargs)
        if "worktree" in args and "add" in args:
            adds.append(result)
            if len(adds) == 1:
                assert result.returncode != 0
                assert not dest.exists()
                # Simulate an OS cleanup remnant after the real failed -b.
                dest.mkdir()
                (dest / "partial.bin").write_text("partial checkout")
        return result

    monkeypatch.setattr(service, "_run_git", traced)
    success, warning = service.add_worktree(str(lfs_repo), str(dest), "partial", new_branch=True)
    assert success, warning
    assert len(adds) == 2 and not (dest / "partial.bin").exists()
    assert (dest / "asset.bin").read_text() == POINTER


def test_present_wrong_branch_is_not_hidden_by_old_git_error():
    from app.core.query_options import WorktreeUnavailableError
    error = WorktreeUnavailableError("session", "feature", mismatch="on main", checkout_error="old failure")
    assert "on the wrong branch" in str(error) and "old failure" not in str(error)


def test_lfs_receipt_is_optional_for_runtime_facts(tmp_path, monkeypatch):
    from fluid.context import runtime_facts as runtime
    monkeypatch.setattr(runtime, "_receipt_path", lambda _: None)
    assert runtime.lfs_fallback_warning(str(tmp_path)) is None
    assert "placeholders" not in runtime.runtime_facts(str(tmp_path))
