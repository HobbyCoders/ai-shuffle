"""Worktree status map: stale-while-revalidate, hot-only refresh, back-off.

Drives ``app.api.git_worktrees._load_worktree_statuses`` directly with a mocked
``git_service`` so timing is under the test's control (the endpoint wiring is
covered in tests/api/test_git.py). The last test uses a real repository with a
zeroed index to check the pygit2 reader's log de-duplication.
"""

import asyncio
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.api import git_worktrees as gw
from app.core import git_native


def _status(branch="main"):
    return {"is_clean": True, "current_branch": branch,
            "modified": [], "staged": [], "untracked": []}


def _wt(i, *, updated_at=None, exists=True):
    sessions = []
    if updated_at is not None:
        sessions.append({"id": f"ses-{i}", "title": "t", "status": "active",
                         "updated_at": updated_at})
    return {"id": f"wt-{i}", "worktree_path": f"/wt/{i}", "exists": exists,
            "sessions": sessions, "git_status": None}


def _old():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def _recent():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def git(monkeypatch):
    """A mocked git_service whose get_status records the paths it read."""
    service = MagicMock()
    service.reads = []

    def get_status(path):
        service.reads.append(path)
        return _status()

    service.get_status.side_effect = get_status
    monkeypatch.setattr(gw, "git_service", service)
    monkeypatch.setattr(gw, "get_worktree_working_dir", lambda wt: wt["worktree_path"])
    monkeypatch.setattr(gw, "_streaming_session_ids", lambda: set())
    gw._worktree_status_cache.clear()
    yield service
    gw._worktree_status_cache.clear()


def _load(worktrees, focus=None, project="p1"):
    asyncio.run(gw._load_worktree_statuses(project, worktrees, focus))
    return worktrees


def _settle(project="p1"):
    """Wait for any in-flight refresh of *project* to finish."""
    state = gw._worktree_status_cache.get(project)
    inflight = state.inflight if state else None
    if inflight is not None:
        inflight.result(timeout=5)


def _make_stale(project="p1"):
    state = gw._worktree_status_cache[project]
    state.refreshed_at = time.monotonic() - (gw._WORKTREE_STATUS_FRESH_SECONDS + 1)


def test_cold_request_waits_for_first_refresh(git):
    rows = _load([_wt(i, updated_at=_old()) for i in range(4)])
    assert sorted(git.reads) == [f"/wt/{i}" for i in range(4)]
    assert all(r["git_status"]["current_branch"] == "main" for r in rows)
    assert all(r["status_checked_at"] and r["status_error"] is None for r in rows)


def test_concurrent_stale_requests_share_one_refresh(git, monkeypatch):
    """N concurrent polls on stale data start ONE refresh and all get the
    cached statuses straight away, while that refresh is still running."""
    worktrees = lambda: [_wt(i, updated_at=_recent()) for i in range(3)]  # noqa: E731
    _load(worktrees())
    _make_stale()
    git.reads.clear()

    release = threading.Event()
    started = threading.Event()

    def slow_status(path):
        started.set()
        release.wait(5)
        git.reads.append(path)
        return _status("new")

    git.get_status.side_effect = slow_status
    submits = []
    real_submit = gw._status_refresh_executor.submit

    def counting_submit(*args, **kwargs):
        submits.append(args)
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(gw._status_refresh_executor, "submit", counting_submit)
    try:
        async def burst():
            rows = [worktrees() for _ in range(8)]
            await asyncio.gather(*(gw._load_worktree_statuses("p1", r) for r in rows))
            return rows

        responses = asyncio.run(burst())
        assert started.wait(5)
        # Served before the refresh finished: every response has the OLD status.
        assert all(r["git_status"]["current_branch"] == "main"
                   for rows in responses for r in rows)
        assert len(submits) == 1
    finally:
        release.set()
    _settle()

    # One refresh read each hot worktree once, not once per request.
    assert sorted(git.reads) == ["/wt/0", "/wt/1", "/wt/2"]
    assert all(r["git_status"]["current_branch"] == "new" for r in _load(worktrees()))


def test_fresh_data_is_not_reread(git):
    _load([_wt(0, updated_at=_recent())])
    git.reads.clear()
    _load([_wt(0, updated_at=_recent())])
    _settle()
    assert git.reads == []


def test_refresh_reads_only_hot_worktrees(git, monkeypatch):
    rows = [
        _wt(0, updated_at=_old()),     # streaming session
        _wt(1, updated_at=_recent()),  # recent activity
        _wt(2, updated_at=_old()),     # focused by the client
        _wt(3, updated_at=_old()),     # idle
        _wt(4),                        # idle, no sessions at all
    ]
    _load(rows)
    _make_stale()
    git.reads.clear()
    monkeypatch.setattr(gw, "_streaming_session_ids", lambda: {"ses-0"})

    served = _load([dict(r) for r in rows] + [_wt(5, updated_at=_old())], focus="wt-2")
    _settle()

    # Hot, focused, and the never-read wt-5; idle ones keep their last status.
    assert sorted(git.reads) == ["/wt/0", "/wt/1", "/wt/2", "/wt/5"]
    assert served[3]["git_status"]["current_branch"] == "main"
    assert served[4]["git_status"]["current_branch"] == "main"


def test_new_worktree_is_read_even_while_fresh(git):
    _load([_wt(0, updated_at=_old())])
    git.reads.clear()
    _load([_wt(0, updated_at=_old()), _wt(1, updated_at=_old())])
    _settle()
    assert git.reads == ["/wt/1"]
    # Read once: once the project is stale again, the idle wt-1 is left alone.
    _make_stale()
    git.reads.clear()
    _load([_wt(0, updated_at=_old()), _wt(1, updated_at=_old())])
    _settle()
    assert git.reads == []


def test_deleted_worktree_drops_out(git):
    _load([_wt(0), _wt(1)])
    _load([_wt(0)])
    assert set(gw._worktree_status_cache["p1"].entries) == {"wt-0"}


def test_missing_on_disk_worktree_gets_no_status(git):
    rows = _load([_wt(0), _wt(1, exists=False)])
    assert git.reads == ["/wt/0"]
    assert rows[1]["git_status"] is None and "status_error" not in rows[1]


def test_broken_worktree_backs_off_then_recovers(git, caplog):
    broken = {"on": True}

    def status(path):
        git.reads.append(path)
        if path == "/wt/1" and broken["on"]:
            raise RuntimeError("invalid data in index - incorrect header signature")
        return _status()

    git.get_status.side_effect = status
    rows = lambda: [_wt(0, updated_at=_recent()), _wt(1, updated_at=_recent())]  # noqa: E731
    caplog.set_level(logging.DEBUG, logger="app.api.git_worktrees")

    served = _load(rows())
    assert served[1]["git_status"] is None
    assert "incorrect header signature" in served[1]["status_error"]
    assert served[0]["status_error"] is None
    entry = gw._worktree_status_cache["p1"].entries["wt-1"]
    assert entry.failures == 1
    first_delay = entry.retry_at - time.monotonic()
    assert 0 < first_delay <= gw._WORKTREE_STATUS_BACKOFF_BASE_SECONDS

    # Stale again, but wt-1 is still backing off: only wt-0 is re-read, even
    # though wt-1 is hot.
    _make_stale()
    git.reads.clear()
    _load(rows())
    _settle()
    assert git.reads == ["/wt/0"]

    # Back-off over, still broken: the next window doubles.
    entry.retry_at = time.monotonic() - 1
    _make_stale()
    git.reads.clear()
    _load(rows())
    _settle()
    assert "/wt/1" in git.reads
    assert entry.failures == 2
    assert entry.retry_at - time.monotonic() > gw._WORKTREE_STATUS_BACKOFF_BASE_SECONDS

    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING and "wt-1" in r.getMessage()]
    assert len(warnings) == 1, "a repeat failure must not log at WARNING again"

    # Fixed on disk: the next retry clears the error and resets the back-off.
    broken["on"] = False
    entry.retry_at = time.monotonic() - 1
    _make_stale()
    served = _load(rows())
    _settle()
    served = _load(rows())
    assert served[1]["git_status"]["is_clean"] is True
    assert served[1]["status_error"] is None
    assert entry.failures == 0
    assert any("recovered" in r.getMessage() for r in caplog.records)


def test_backoff_is_capped(git):
    git.get_status.side_effect = RuntimeError("boom")
    _load([_wt(0)])
    entry = gw._worktree_status_cache["p1"].entries["wt-0"]
    for _ in range(40):
        gw._record_status(entry, "wt-0", None, "boom")
    assert entry.retry_at - time.monotonic() <= gw._WORKTREE_STATUS_BACKOFF_MAX_SECONDS


def test_native_partial_status_counts_as_failure(git, monkeypatch):
    """pygit2 hands back a partial 'clean' dict on failure; it must surface as
    an error, not as a clean worktree."""
    monkeypatch.setitem(git_native._status_errors, "/wt/0", "incorrect header signature")
    rows = _load([_wt(0)])
    assert rows[0]["git_status"] is None
    assert rows[0]["status_error"] == "incorrect header signature"


def test_invalidation_rereads_one_worktree_and_resets_backoff(git):
    git.get_status.side_effect = RuntimeError("boom")
    _load([_wt(0), _wt(1)])
    assert gw._worktree_status_cache["p1"].entries["wt-0"].failures == 1

    git.get_status.side_effect = lambda path: git.reads.append(path) or _status()
    git.reads.clear()
    gw._invalidate_worktree_status_cache("p1", "wt-0")
    rows = _load([_wt(0), _wt(1)])
    _settle()
    # wt-0 is read afresh despite its back-off; wt-1 still waits out its own.
    assert git.reads == ["/wt/0"]
    assert _load([_wt(0), _wt(1)])[0]["status_error"] is None
    assert rows[1]["status_error"] == "boom"


def test_refresh_started_before_invalidation_does_not_write_back(git):
    _load([_wt(0)])
    state = gw._worktree_status_cache["p1"]
    start_gen = state.generation
    gw._invalidate_worktree_status_cache("p1", "wt-0")
    gw._refresh_project_statuses(state, [_wt(0)], start_gen)
    assert "wt-0" not in state.entries


def test_full_invalidation_makes_project_cold(git):
    _load([_wt(0)])
    gw._invalidate_worktree_status_cache("p1")
    assert "p1" not in gw._worktree_status_cache


@pytest.mark.skipif(not git_native.HAS_PYGIT2, reason="pygit2 not installed")
def test_native_reader_logs_a_corrupt_index_once(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    index = repo / ".git" / "index"
    index.write_bytes(b"\0" * 64)

    reader = git_native.GitNativeReader()
    path = str(repo)
    caplog.set_level(logging.DEBUG, logger="app.core.git_native")
    try:
        reader.get_status(path)
        reader.get_status(path)
        errors = [r for r in caplog.records
                  if r.levelno == logging.ERROR and "pygit2" in r.getMessage()]
        assert len(errors) == 1
        assert git_native.get_status_error(path)

        index.unlink()
        reader.get_status(path)
        assert git_native.get_status_error(path) is None
    finally:
        git_native._status_errors.pop(path, None)
