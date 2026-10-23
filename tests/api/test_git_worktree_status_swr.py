"""Worktree status map: stale-while-revalidate, hot-only refresh, back-off.

Drives ``app.api.git_worktrees._load_worktree_statuses`` directly with a mocked
``git_service`` so timing is under the test's control (the endpoint wiring is
covered in tests/api/test_git.py). The last tests use real repositories: a
zeroed index (one warning, then recovery) and a checkout invalidated by a
turn end.
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


def _status(branch="main"):
    return {"is_clean": True, "current_branch": branch,
            "modified": [], "staged": [], "untracked": []}


def _wt(i, *, updated_at="recent", exists=True):
    if updated_at == "recent":
        updated_at = _recent()
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
    service.errors = {}
    service.generations = {}
    service.get_status_error.side_effect = lambda path: service.errors.get(path)
    service.status_generation.side_effect = lambda path: service.generations.get(path, 0)

    def get_status(path):
        service.reads.append(path)
        return _status()

    service.get_status.side_effect = get_status
    from app.core import checkout_manager
    monkeypatch.setattr(gw, "checkout_events", MagicMock())
    monkeypatch.setattr(checkout_manager, "_require_checkout_root", lambda path: None)
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
    rows = _load([_wt(i, updated_at=_recent()) for i in range(4)])
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
        _wt(4, updated_at=None),       # idle, no sessions at all
    ]
    _load([_wt(i) for i in range(5)])  # known statuses from earlier activity
    _make_stale()
    git.reads.clear()
    monkeypatch.setattr(gw, "_streaming_session_ids", lambda: {"ses-0"})

    served = _load([dict(r) for r in rows] + [_wt(5, updated_at=_old())], focus="wt-2")
    _settle()

    # Hot/focused only; even never-read historical rows stay not checked.
    assert sorted(git.reads) == ["/wt/0", "/wt/1", "/wt/2"]
    assert served[5]["git_status"] is None
    assert served[5]["status_checked_at"] is None
    assert served[3]["git_status"]["current_branch"] == "main"
    assert served[4]["git_status"]["current_branch"] == "main"


def test_unseen_historical_worktree_is_read_only_on_demand(git):
    _load([_wt(0, updated_at=_old())])
    rows = [_wt(0, updated_at=_old()), _wt(1, updated_at=_old())]
    _load(rows)
    _settle()
    assert git.reads == []
    assert all(row["git_status"] is None and row["status_checked_at"] is None for row in rows)
    _load(rows, focus="wt-1")
    _settle()
    assert git.reads == ["/wt/1"]
    _make_stale()
    git.reads.clear()
    _load(rows)
    _settle()
    assert git.reads == []


def test_100_historical_and_three_active_scale_from_cold(git, monkeypatch):
    rows = [_wt(i, updated_at=_old()) for i in range(100)] + [
        _wt(100, updated_at=_old()), _wt(101), _wt(102, updated_at=_old()),
    ]
    monkeypatch.setattr(gw, "_streaming_session_ids", lambda: {"ses-102"})
    for _ in range(3):
        _load(rows, focus="wt-100")
        _settle()
        assert set(git.reads) == {"/wt/100", "/wt/101", "/wt/102"}
        assert len(git.reads) == 3
        assert all(row["status_checked_at"] is None for row in rows[:100])
        gw.checkout_events.update.assert_called_with("p1", {"/wt/100", "/wt/101", "/wt/102"})
        _make_stale()
        git.reads.clear()


def test_explicit_invalidation_checks_unseen_historical_row_once(git):
    rows = [_wt(0, updated_at=_old()), _wt(1, updated_at=_old())]
    _load(rows)
    gw._invalidate_worktree_status_cache("p1", "wt-1")
    _load(rows)
    _settle()
    assert git.reads == ["/wt/1"]
    assert not gw._worktree_status_cache["p1"].invalidated_gen
    _make_stale()
    _load(rows)
    _settle()
    assert git.reads == ["/wt/1"]


def test_inactive_failure_does_not_retry_just_because_backoff_expired(git):
    git.get_status.side_effect = RuntimeError("boom")
    _load([_wt(0)])
    entry = gw._worktree_status_cache["p1"].entries["wt-0"]
    entry.retry_at = 0
    _make_stale()
    git.get_status.reset_mock()
    _load([_wt(0, updated_at=_old())])
    _settle()
    git.get_status.assert_not_called()


def test_deleted_worktree_drops_out(git):
    _load([_wt(0), _wt(1)])
    _load([_wt(0)])
    assert set(gw._worktree_status_cache["p1"].entries) == {"wt-0"}


def test_missing_on_disk_worktree_gets_no_status(git):
    rows = _load([_wt(0), _wt(1, exists=False)])
    assert git.reads == ["/wt/0"]
    assert rows[1]["git_status"] is None
    assert "needs cleanup" in rows[1]["status_error"]


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
    # No subtraction: on Windows both clock reads can land in one ~15.6 ms
    # tick, and (t + 1800) - t rounds to 1800.0000000000002.
    assert entry.retry_at <= time.monotonic() + gw._WORKTREE_STATUS_BACKOFF_MAX_SECONDS


def test_failed_read_fallback_counts_as_failure(git):
    """get_status hands back the empty dict on failure and records why; that
    must surface as an error, not as a clean worktree."""
    git.errors["/wt/0"] = "incorrect header signature"
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


def test_retired_worktrees_are_never_read(git):
    """Finished rows stay listed, but the fan-out leaves them alone (#436)."""
    rows = [_wt(0), _wt(1), _wt(2), _wt(3)]
    rows[1]["status"] = "completed"
    rows[2]["status"] = "archived"
    rows[3]["status"] = "deleted"
    served = _load(rows)
    assert git.reads == ["/wt/0"]
    assert [r["git_status"] is None for r in served] == [False, True, True, True]


def test_invalidated_idle_worktree_is_reread_while_fresh(git):
    """A turn end or Git action in an idle worktree's checkout makes it due at
    once, without waiting for the project to go stale or the worktree to be hot."""
    _load([_wt(0), _wt(1)])
    git.reads.clear()
    _load([_wt(0, updated_at=_old()), _wt(1, updated_at=_old())])
    _settle()
    assert git.reads == []

    git.generations["/wt/1"] = 1  # git_service.invalidate_status_cache("/wt/1")
    git.get_status.side_effect = lambda path: git.reads.append(path) or _status("edited")
    _load([_wt(0, updated_at=_old()), _wt(1, updated_at=_old())])
    _settle()
    assert git.reads == ["/wt/1"]
    served = _load([_wt(0, updated_at=_old()), _wt(1, updated_at=_old())])
    assert served[1]["git_status"]["current_branch"] == "edited"
    _settle()
    assert git.reads == ["/wt/1"], "read once per invalidation, not on every poll"


def test_corrupt_index_warns_once_then_recovers(tmp_path, caplog, monkeypatch):
    from app.core.git_service import GitService
    from tests.git_repo_helpers import hermetic_git_env

    hermetic_git_env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    index = repo / ".git" / "index"
    index.write_bytes(b"\0" * 64)

    service = GitService()
    path = str(repo)
    caplog.set_level(logging.DEBUG, logger="app.core.git_service")
    service.invalidate_status_cache(path)
    for _ in range(2):
        status = service.get_status(path)
        assert status["is_git_repo"] is False
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "git status failed" in r.getMessage()]
    assert len(warnings) == 1
    assert service.get_status_error(path)

    index.unlink()
    status = service.get_status(path)
    assert status["is_git_repo"] is True
    assert service.get_status_error(path) is None



def test_idle_edit_invalidates_real_status_after_debounce(tmp_path, monkeypatch):
    """Real file event -> service generation -> SWR reread, not a seeded cache."""
    from app.core import checkout_events as ce
    from app.core.git_service import GitService
    from tests.git_repo_helpers import hermetic_git_env

    hermetic_git_env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    (repo / "tracked.txt").write_text("before")
    run("add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    service = GitService()
    invalidated = threading.Event()
    ready = threading.Event()
    native_watch = ce.watch

    def observed_watch(*args, **kwargs):
        # A timeout means native registration has completed, without guessing
        # how long registration takes on a loaded Windows/Linux runner.
        for changes in native_watch(*args, **kwargs):
            ready.set()
            yield changes

    def invalidate(path):
        service.invalidate_status_cache(path)
        invalidated.set()

    events = ce.CheckoutEvents(invalidate)
    monkeypatch.setattr(ce, "watch", observed_watch)
    monkeypatch.setattr(gw, "checkout_events", events)
    monkeypatch.setattr(gw, "git_service", service)
    monkeypatch.setattr(gw, "_streaming_session_ids", lambda: set())
    rows = [_wt(0)]
    rows[0]["worktree_path"] = str(repo)
    gw._worktree_status_cache.clear()
    try:
        first = _load(rows)[0]["status_checked_at"]
        assert rows[0]["git_status"]["is_clean"]
        assert ready.wait(10), "native watch did not register"
        # Session is idle (not streaming) but recently active. No turn-end hook.
        (repo / "tracked.txt").write_text("after")
        assert invalidated.wait(10), "file event did not invalidate status"
        _load(rows)
        _settle()
        _load(rows)
        assert "tracked.txt" in rows[0]["git_status"]["modified"]
        assert rows[0]["status_checked_at"] > first
    finally:
        events.close()
        gw._worktree_status_cache.clear()


def test_checkout_watches_prune_history_share_and_expire(tmp_path, monkeypatch):
    from app.core import checkout_events as ce

    root = tmp_path / "active"
    root.mkdir()
    (root / "src").mkdir()
    for name in (".fluid", ".git", "node_modules"):
        (root / name).mkdir()
        (root / name / "historical").mkdir()
    assert set(ce._directories(str(root))) == {str(root), str(root / "src")}
    registered = threading.Event()
    stopped = threading.Event()

    def fake_watch(*paths, stop_event, **kwargs):
        assert set(paths) == {str(root), str(root / "src")}
        assert kwargs["recursive"] is False
        assert kwargs["debounce"] == kwargs["step"] == ce._DEBOUNCE_MS
        registered.set()
        stop_event.wait(5)
        stopped.set()
        return
        yield  # make this a generator

    monkeypatch.setattr(ce, "watch", fake_watch)
    events = ce.CheckoutEvents(MagicMock())
    try:
        events.update("p1", {str(root)})
        assert registered.wait(5)
        entry = events._watches[str(root)]
        events.update("p2", {str(root)})
        events.update("p1", set())
        assert events._watches[str(root)] is entry
        assert not entry.stop.is_set()
        # A disappearing client stops owning its checkout after the lease.
        with events._lock:
            entry.owners["p2"] = time.monotonic() - 1
        assert not events._live(entry)
        events.update("p2", set())
        assert stopped.wait(5)
        assert not events._watches
    finally:
        events.close()
