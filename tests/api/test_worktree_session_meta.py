"""
Tests for ``enrich_session_worktree_meta`` — the shared helper that annotates a
session poll with worktree metadata.

Contract: ``worktree_deleted`` is True only when the owner's tree is not
materialized — no active checkout among all rows for (project_v2, owner, branch),
no in-flight ``ensure_worktree``, and (local only) no directory on this disk.
Remote sessions never stat the viewing install. Resolver/DB errors fail open
to False. The worktree row's ``status`` is never consulted.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import app.api.worktree_session_meta as wsm
import app.core.project_utils as pu
from app.api.worktree_session_meta import enrich_session_worktree_meta
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import git_ops as git_ops_db
from app.db import projects_v2
from tests.fake_path import FakePath

ME = "local-me"
PEER = "peer-install-1"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


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


# ---------------------------------------------------------------------------
# Cluster honesty: active checkouts + in-flight git_ops, not the viewer's disk.
# ---------------------------------------------------------------------------

def _pv2():
    return projects_v2.upsert_project("git:test/worktree-meta", name="Repo")


def _remote_session(pv2, **over):
    base = {
        "id": "ses-1",
        "worktree_id": "wt-1",
        "worktree_branch": "feat/foo",
        "worktree_path": ".worktrees/feat/foo",
        "branch": "feat/foo",
        "project_v2_id": pv2["id"],
        "instance_id": PEER,
    }
    base.update(over)
    return base


def _local_v2_session(pv2, **over):
    base = {
        "id": "ses-1",
        "worktree_id": None,
        "worktree_branch": None,
        "worktree_path": None,
        "branch": "feat/foo",
        "project_v2_id": pv2["id"],
        "instance_id": ME,
    }
    base.update(over)
    return base


def _queue(pv2, runner=PEER, branch="feat/foo"):
    return git_ops_db.insert_op(
        runner,
        "ensure_worktree",
        project_v2_id=pv2["id"],
        params={"branch": branch, "base_branch": "main", "create_new_branch": True},
    )


def _backdate(op_id: str, minutes: int):
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE git_ops SET updated_at = %s WHERE id = %s", (old, op_id)
        )


def test_remote_archived_oldest_plus_newer_active_is_not_deleted(monkeypatch):
    """get_checkout is LIMIT 1 oldest — an archived row must not hide the live one."""
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    old = checkouts_db.upsert_checkout(
        pv2["id"], PEER, "/old/feat/foo", branch="feat/foo"
    )
    checkouts_db.set_checkout_status(old["id"], "archived")
    checkouts_db.upsert_checkout(
        pv2["id"], PEER, "/new/feat/foo", branch="feat/foo"
    )
    meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is False


def test_remote_no_active_checkout_is_deleted(monkeypatch):
    """No active row on the owner, nothing in-flight → deleted. Viewer disk is irrelevant."""
    _patch(monkeypatch, existing={"/abs/.worktrees/feat/foo"})
    pv2 = _pv2()
    old = checkouts_db.upsert_checkout(
        pv2["id"], PEER, "/old/feat/foo", branch="feat/foo"
    )
    checkouts_db.set_checkout_status(old["id"], "archived")
    calls = {"n": 0}
    real_is_dir = FakePath.is_dir

    def counting_is_dir(self):
        calls["n"] += 1
        return real_is_dir(self)

    monkeypatch.setattr(FakePath, "is_dir", counting_is_dir)
    meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is True
    assert calls["n"] == 0


def test_remote_queued_ensure_worktree_is_not_deleted(monkeypatch):
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    _queue(pv2)
    meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is False


def test_remote_running_ensure_worktree_is_not_deleted(monkeypatch):
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    op = _queue(pv2)
    claimed = git_ops_db.claim_op(op["id"], PEER)
    assert claimed is not None
    meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is False


def test_zombie_running_plus_newer_queued_is_not_deleted(monkeypatch):
    """find_matching skips stale running and returns the queued retry — inflight."""
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    zombie = _queue(pv2)
    git_ops_db.claim_op(zombie["id"], PEER)
    _backdate(zombie["id"], minutes=180)
    _queue(pv2)
    meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is False


def test_metadata_path_never_calls_fail_op(monkeypatch):
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    zombie = _queue(pv2)
    git_ops_db.claim_op(zombie["id"], PEER)
    _backdate(zombie["id"], minutes=20)
    with patch.object(git_ops_db, "fail_op") as fail:
        meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
        fail.assert_not_called()
    # Zombie only, no queued retry, no active checkout → deleted, still no write.
    assert meta["worktree_deleted"] is True


def test_local_checkout_disk_exists_is_not_deleted(monkeypatch):
    """Local: TTL disk stat across active checkout paths, not row presence alone."""
    _patch(monkeypatch, existing={"/work/feat/foo"})
    pv2 = _pv2()
    checkouts_db.upsert_checkout(
        pv2["id"], ME, "/work/feat/foo", branch="feat/foo"
    )
    meta = enrich_session_worktree_meta(_local_v2_session(pv2), None)
    assert meta["worktree_deleted"] is False


def test_local_checkout_disk_missing_is_deleted(monkeypatch):
    """v2 branch uses checkouts, not path None → False."""
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    checkouts_db.upsert_checkout(
        pv2["id"], ME, "/work/feat/foo", branch="feat/foo"
    )
    meta = enrich_session_worktree_meta(_local_v2_session(pv2), None)
    assert meta["worktree_deleted"] is True


def test_checkout_resolver_error_falls_back_to_not_deleted(monkeypatch):
    _patch(monkeypatch, existing=set())
    pv2 = _pv2()
    checkouts_db.upsert_checkout(
        pv2["id"], PEER, "/work/feat/foo", branch="feat/foo"
    )
    with patch.object(
        checkouts_db, "has_active_checkout", side_effect=RuntimeError("db down")
    ):
        meta = enrich_session_worktree_meta(_remote_session(pv2), _wt())
    assert meta["worktree_deleted"] is False
