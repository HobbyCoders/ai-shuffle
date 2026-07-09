"""
Helper-level tests for ``resolve_existing_worktree_cwd`` — the ONE shared rule
for resolving a worktree session's cwd: try the worktree row's path, then the
session's backup ``worktree_path``; the first candidate whose directory exists
on disk wins; status is never consulted; the function never raises.

Absolute candidate paths are used so the real ``get_worktree_working_dir``
returns them verbatim (no ``settings.workspace_dir`` dependence), and existence
is controlled through the injectable ``is_dir`` predicate — no filesystem, no
``Path`` patching.
"""

import app.core.project_utils as pu
from app.core.project_utils import resolve_existing_worktree_cwd

ROW_PATH = "/wt/row-path"
BACKUP_PATH = "/wt/backup-path"


def _row(path=ROW_PATH):
    return {"id": "wt-1", "worktree_path": path, "status": "deleted"}


def _session(path=BACKUP_PATH):
    return {"id": "ses-1", "worktree_id": "wt-1", "worktree_path": path}


def _exists(*paths):
    on_disk = set(paths)
    return lambda p: p in on_disk


# ---------------------------------------------------------------------------

def test_row_path_wins_when_both_exist():
    result = resolve_existing_worktree_cwd(
        _row(), _session(), is_dir=_exists(ROW_PATH, BACKUP_PATH)
    )
    assert result == ROW_PATH


def test_backup_used_when_row_path_missing():
    result = resolve_existing_worktree_cwd(
        _row(), _session(), is_dir=_exists(BACKUP_PATH)
    )
    assert result == BACKUP_PATH


def test_row_only_no_session():
    assert resolve_existing_worktree_cwd(_row(), None, is_dir=_exists(ROW_PATH)) == ROW_PATH


def test_backup_only_no_row():
    assert (
        resolve_existing_worktree_cwd(None, _session(), is_dir=_exists(BACKUP_PATH))
        == BACKUP_PATH
    )


def test_none_exist_returns_none():
    assert resolve_existing_worktree_cwd(_row(), _session(), is_dir=_exists()) is None


def test_no_candidates_returns_none():
    assert resolve_existing_worktree_cwd(None, None, is_dir=_exists(ROW_PATH)) is None
    assert (
        resolve_existing_worktree_cwd(
            {"id": "wt-1", "worktree_path": None},
            {"id": "ses-1", "worktree_path": ""},
            is_dir=_exists(ROW_PATH),
        )
        is None
    )


def test_raising_first_candidate_does_not_mask_backup(monkeypatch):
    """Regression: per-candidate try/except — a corrupt/unresolvable row path
    must fall through to the session's backup path, not abort resolution."""
    real = pu.get_worktree_working_dir

    def flaky(candidate):
        if candidate.get("worktree_path") == ROW_PATH:
            raise ValueError("corrupt row path")
        return real(candidate)

    monkeypatch.setattr(pu, "get_worktree_working_dir", flaky)
    result = resolve_existing_worktree_cwd(
        _row(), _session(), is_dir=_exists(BACKUP_PATH)
    )
    assert result == BACKUP_PATH


def test_never_raises_when_all_candidates_raise(monkeypatch):
    def boom(_candidate):
        raise ValueError("corrupt")

    monkeypatch.setattr(pu, "get_worktree_working_dir", boom)
    assert (
        resolve_existing_worktree_cwd(_row(), _session(), is_dir=_exists(ROW_PATH))
        is None
    )


def test_identical_row_and_backup_paths_stat_once():
    calls = []

    def counting_is_dir(p):
        calls.append(p)
        return False

    result = resolve_existing_worktree_cwd(
        _row(ROW_PATH), _session(ROW_PATH), is_dir=counting_is_dir
    )
    assert result is None
    # Deduped: the shared path is checked exactly once.
    assert calls == [ROW_PATH]
