"""Real Linux bind mounts, isolated so failed checks cannot endanger temp cleanup."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.git_repo_helpers import git, hermetic_git_env, init_repo, write


@pytest.mark.skipif(sys.platform != "linux", reason="Linux mount namespaces and bind mounts")
@pytest.mark.parametrize("operation", ["forget", "remove"])
def test_worktree_operation_refuses_real_bind_mount(tmp_path, monkeypatch, operation):
    for tool in ("unshare", "mount"):
        if shutil.which(tool) is None:
            pytest.skip(f"real bind-mount test requires {tool}")
    # Namespace teardown releases mounts even if the child crashes. Never leave
    # pytest's recursive tmp_path cleanup looking at a mounted target.
    probe = subprocess.run(
        ["unshare", "--mount", "--propagation", "private", "true"],
        capture_output=True, text=True, timeout=10,
    )
    if probe.returncode:
        if "Operation not permitted" in probe.stderr or "Permission denied" in probe.stderr:
            pytest.skip("private mount namespace unavailable (requires CAP_SYS_ADMIN)")
        pytest.fail(f"mount namespace probe failed: {probe.stderr}")
    hermetic_git_env(monkeypatch, tmp_path)
    result = subprocess.run(
        [
            "unshare", "--mount", "--propagation", "private", sys.executable, "-c",
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_exercise_mount'](Path(sys.argv[2]), sys.argv[3])",
            str(Path(__file__).resolve()), str(tmp_path), operation,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 77:
        pytest.skip("real bind mount unavailable (requires CAP_SYS_ADMIN)")
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_mount(tmp_path, operation):
    from app.core.git_service import git_service
    from app.core.git_worktree_ops import forget_stale_worktree_metadata

    repo = init_repo(tmp_path / "main")
    checkout = repo / ".fluid" / "worktrees" / "topic"
    git(repo, "worktree", "add", "-q", "-b", "topic", str(checkout))
    admin = Path(git(checkout, "rev-parse", "--absolute-git-dir"))
    # A second real checkout, on the same filesystem: st_dev alone cannot
    # distinguish this bind mount. Spaces also exercise mountinfo unescaping.
    other = init_repo(tmp_path / "other checkout", {"nested/keep.txt": "precious\n"})
    before = {p.relative_to(other): p.read_bytes() for p in other.rglob("*") if p.is_file()}
    mountpoint = (admin if operation == "forget" else checkout) / "nested" / "unexpected mount"
    mountpoint.mkdir(parents=True)
    metadata_before = {p.relative_to(admin): p.read_bytes() for p in admin.rglob("*") if p.is_file()}
    mounted = subprocess.run(
        ["mount", "--bind", str(other), str(mountpoint)], capture_output=True, text=True,
    )
    if mounted.returncode:
        if "permission denied" in mounted.stderr.lower() or "operation not permitted" in mounted.stderr.lower():
            raise SystemExit(77)
        raise AssertionError(mounted.stderr)
    assert mountpoint.stat().st_dev == other.stat().st_dev == admin.stat().st_dev
    assert (mountpoint / "nested" / "keep.txt").read_text() == "precious\n"
    if operation == "forget":
        (checkout / ".git").unlink()
        with pytest.raises(ValueError, match="mount"):
            forget_stale_worktree_metadata(str(repo), str(checkout))
    else:
        assert git_service.remove_worktree(str(repo), str(checkout), force=True) is False
    assert checkout.is_dir() and admin.is_dir()
    for rel, contents in metadata_before.items():
        assert (admin / rel).read_bytes() == contents
    assert {p.relative_to(other): p.read_bytes() for p in other.rglob("*") if p.is_file()} == before
    assert str(checkout) in git(repo, "worktree", "list", "--porcelain")
    assert git(repo, "rev-parse", "topic")
