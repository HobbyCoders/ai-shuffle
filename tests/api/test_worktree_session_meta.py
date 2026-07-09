"""
Tests for ``enrich_session_worktree_meta`` — the shared helper that annotates a
session poll with worktree metadata.

Contract: ``worktree_deleted`` reflects ACTUAL disk existence of the resolved
worktree directory (a single cheap stat), NOT the row's ``status``. Status is
intentionally ignored so a per-device ``sync-worktrees`` sweep that flipped a
healthy row to 'deleted' can't scare the UI while the directory is right there.
Any resolution/stat failure falls back to ``False``.
"""

import app.api.worktree_session_meta as wsm
import app.core.project_utils as pu
from app.api.worktree_session_meta import enrich_session_worktree_meta
from tests.fake_path import FakePath


def _patch(monkeypatch, existing=None, resolver=None):
    FakePath.existing = set(existing or [])
    # The TTL stat cache is module-level state — never let it leak across tests.
    wsm._is_dir_cache.clear()
    _resolver = resolver or (lambda wt: f"/abs/{wt['worktree_path']}")
    # _is_dir_cached stats via the wsm Path binding; the shared rule
    # (resolve_existing_worktree_cwd) resolves via the project_utils bindings.
    # Patch both namespaces so the fake filesystem governs everything.
    monkeypatch.setattr(wsm, "Path", FakePath)
    monkeypatch.setattr(wsm, "get_worktree_working_dir", _resolver)
    monkeypatch.setattr(pu, "Path", FakePath)
    monkeypatch.setattr(pu, "get_worktree_working_dir", _resolver)


def _wt(status="active", branch="feat/foo"):
    return {
        "id": "wt-1",
        "status": status,
        "branch_name": branch,
        "worktree_path": f".worktrees/{branch}",
    }


def _session(**over):
    base = {
        "id": "ses-1",
        "worktree_id": "wt-1",
        "worktree_branch": "feat/foo",
        "worktree_path": ".worktrees/feat/foo",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------

def test_non_worktree_session_returns_empty(monkeypatch):
    _patch(monkeypatch)
    session = {"id": "ses-1", "worktree_id": None, "worktree_branch": None, "worktree_path": None}
    assert enrich_session_worktree_meta(session, None) == {}


def test_dir_exists_is_not_deleted(monkeypatch):
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    meta = enrich_session_worktree_meta(_session(), _wt(status="active"))
    assert meta["worktree_deleted"] is False
    assert meta["execution_mode"] == "worktree"
    assert meta["worktree_id"] == "wt-1"
    assert meta["worktree_branch"] == "feat/foo"


def test_dir_exists_but_stale_status_is_not_deleted(monkeypatch):
    """Disk-based: a 'deleted' status with the dir present is NOT reported deleted."""
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    meta = enrich_session_worktree_meta(_session(), _wt(status="deleted"))
    assert meta["worktree_deleted"] is False


def test_dir_missing_is_deleted(monkeypatch):
    """Disk-based: an 'active' status with the dir gone IS reported deleted."""
    _patch(monkeypatch, existing=set())
    meta = enrich_session_worktree_meta(_session(), _wt(status="active"))
    assert meta["worktree_deleted"] is True


def test_no_row_uses_session_backup_path(monkeypatch):
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    meta = enrich_session_worktree_meta(_session(), None)
    assert meta["worktree_deleted"] is False
    assert meta["worktree_path"] == ".worktrees/feat/foo"
    # worktree_id is never nulled — the session stays reopenable.
    assert meta["worktree_id"] == "wt-1"


def test_resolution_error_falls_back_to_not_deleted(monkeypatch):
    def _boom(_wt_dict):
        raise ValueError("corrupt worktree_path")

    _patch(monkeypatch, existing=set(), resolver=_boom)
    meta = enrich_session_worktree_meta(_session(), _wt(status="active"))
    # A transient/resolution error must not scare the UI.
    assert meta["worktree_deleted"] is False


def test_row_path_missing_but_backup_exists_is_not_deleted(monkeypatch):
    """Same shared rule as the resume path: deleted only when NO candidate dir
    exists — the session's backup path participates."""
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    wt = _wt(status="active")
    wt["worktree_path"] = ".worktrees/feat/foo-recreated"  # rewritten row, gone
    meta = enrich_session_worktree_meta(_session(), wt)
    assert meta["worktree_deleted"] is False


def test_is_dir_stat_is_ttl_cached(monkeypatch):
    """/live polls every 150ms — the disk stat must be memoized, not per-poll."""
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    calls = {"n": 0}
    real_is_dir = FakePath.is_dir

    def counting_is_dir(self):
        calls["n"] += 1
        return real_is_dir(self)

    monkeypatch.setattr(FakePath, "is_dir", counting_is_dir)
    for _ in range(5):
        meta = enrich_session_worktree_meta(_session(), _wt(status="active"))
        assert meta["worktree_deleted"] is False
    # One real stat for the path; the other four polls hit the TTL cache.
    assert calls["n"] == 1
