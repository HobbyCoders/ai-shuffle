import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_saturated_journal_bounds_reaps_and_warning_rate(tmp_path, monkeypatch, caplog):
    import fluid.codemode.skills as module
    store = module.SkillStore(tmp_path / "skills")
    before = module.journal_metrics()
    monkeypatch.setattr(module, "JOURNAL_MAX_BYTES", 1)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    reaps = []
    monkeypatch.setattr(store, "reap_journal", lambda: reaps.append(clock.now))
    with caplog.at_level(logging.WARNING):
        for _ in range(100):
            store.journal_append({"kind": "inference", "content": "x"})
        assert len(reaps) == 1
        assert len([r for r in caplog.records if "byte quota" in r.message]) == 1
        clock.now += 61
        store.journal_append({"kind": "inference", "content": "x"})
    assert len(reaps) == 2
    assert len([r for r in caplog.records if "byte quota" in r.message]) == 2
    with pytest.raises(OSError, match="quota"):
        store.journal_append({"kind": "remember", "content": "explicit must surface failure"})
    assert len(reaps) == 3  # Explicit entries never skip their reclamation attempt.
    observed = module.journal_metrics()
    assert observed["skipped_quota"] - before["skipped_quota"] == 101
    assert observed["explicit_quota_failures"] - before["explicit_quota_failures"] == 1
    assert observed["saturation_observed_last_60s"] is True
    clock.now += 61
    assert module.journal_metrics()["saturation_observed_last_60s"] is False


@pytest.mark.asyncio
async def test_access_request_lifecycle_broadcasts_scoped_refresh(monkeypatch):
    from app.core import chat_access
    from app.core.sync_engine import sync_engine
    owner = ("project", "worker", "user-7", "created")
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=owner))
    send = AsyncMock()
    monkeypatch.setattr(sync_engine, "_broadcast_user_message", send)
    broker = chat_access.Broker()
    task = asyncio.create_task(broker.request("session-7", "machine", "Verify an isolated test target"))
    await asyncio.sleep(0)
    await asyncio.gather(*broker._notifications)
    # The bare waiting count lets the sidebar mark a session that is not open (#1807).
    send.assert_awaited_with("user-7", {"type": "chat_access_changed", "session_id": "session-7", "pending": 1})
    request_id = next(iter(broker.requests))
    await broker.decline("session-7", request_id)
    with pytest.raises(chat_access.AccessDenied, match="declined"):
        await task
    await asyncio.gather(*broker._notifications)
    assert send.await_count == 2
    send.assert_awaited_with("user-7", {"type": "chat_access_changed", "session_id": "session-7", "pending": 0})
    assert not broker.requests


@pytest.mark.asyncio
async def test_expired_access_does_not_create_refresh_feedback(monkeypatch):
    from app.core import chat_access, chat_vault
    owner = ("project", "worker", "user-7", "created")
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(chat_vault, "catalog", lambda user, *, cache_empty: [])
    broker = chat_access.Broker()
    grant = chat_access.Grant(id="expired", session_id="session-7", owner=owner, label="fixture", kind="folder", target="fixture", expires_at=0)
    broker.grants[grant.id] = grant
    from unittest.mock import Mock
    notify = Mock()
    monkeypatch.setattr(broker, "_notify", notify)
    for _ in range(10):
        assert (await broker.snapshot("session-7"))["grants"] == []
    notify.assert_called_once_with("session-7", owner)


@pytest.mark.asyncio
async def test_empty_access_snapshot_authorizes_once(monkeypatch):
    from app.core import chat_access, chat_vault
    owner = ("project", "worker", "user-7", "created")
    get_owner = AsyncMock(return_value=owner)
    monkeypatch.setattr(chat_access, "session_owner", get_owner)
    monkeypatch.setattr(chat_vault, "catalog", lambda user, *, cache_empty: [])
    result = await chat_access.Broker().snapshot("session-7")
    assert result["grants"] == []
    get_owner.assert_awaited_once_with("session-7")


@pytest.mark.asyncio
async def test_empty_access_endpoint_reads_session_once_and_caches_empty_catalog(monkeypatch):
    """The panel's snapshot request authorizes the session row once and does
    no repeated vault-catalog query while the owner has nothing saved."""
    from contextlib import contextmanager

    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import chat_access as api
    from app.core import chat_access, chat_vault, encryption
    from app.db import database
    from app.network import control
    chat_vault._invalidate_catalog()
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(control, "owner_of", lambda row: None)
    session_reads, catalog_reads = [], []
    def get_session(session_id):
        session_reads.append(session_id)
        return {"id": session_id, "mode": "chat", "instance_id": "worker"}
    monkeypatch.setattr(database, "get_session", get_session)
    class Cursor:
        def execute(self, sql, params):
            catalog_reads.append(params)
        def fetchall(self):
            return []
    @contextmanager
    def db():
        yield SimpleNamespace(cursor=Cursor)
    monkeypatch.setattr(chat_vault.database, "get_db", db)
    monkeypatch.setattr(api, "broker", chat_access.Broker())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="https://test") as client:
            for _ in range(10):
                response = await client.get("/api/v1/chat-access/session-7")
                assert response.status_code == 200, response.text
                assert response.json()["saved_connections"] == []
    finally:
        chat_vault._invalidate_catalog()
    assert session_reads == ["session-7"] * 10
    assert len(catalog_reads) == 1


@pytest.mark.asyncio
async def test_access_endpoint_still_refuses_unsupported_session_from_loaded_row(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import chat_access as api
    from app.core import chat_access, chat_vault
    from app.db import database
    from app.network import chat_access_routing as routing
    from app.network import control
    catalog, forwarded = [], []
    monkeypatch.setattr(chat_vault, "catalog", lambda *args, **kwargs: catalog.append(args) or [])
    monkeypatch.setattr(control, "owner_of", lambda row: row.get("owner_device_id"))

    async def forward(owner, session_id, operation, payload=None, **_):
        forwarded.append((owner, session_id, operation))
        return dict(routing.EMPTY_SNAPSHOT)

    monkeypatch.setattr(routing, "forward", forward)
    rows = {"unknown-mode": {"mode": "unexpected"}, "mirrored": {"mode": "chat", "owner_device_id": "peer"}}
    monkeypatch.setattr(database, "get_session", lambda session_id: rows[session_id])
    monkeypatch.setattr(api, "broker", chat_access.Broker())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    async with AsyncClient(transport=ASGITransport(app), base_url="https://test") as client:
        assert (await client.get("/api/v1/chat-access/unknown-mode")).status_code == 409
        # A mirrored session's access lives on its owner: forwarded, never served from this vault.
        assert (await client.get("/api/v1/chat-access/mirrored")).status_code == 200
    assert forwarded == [("peer", "mirrored", "snapshot")]
    assert catalog == []




@pytest.mark.asyncio
async def test_whole_stack_denies_non_owner_before_collecting(monkeypatch):
    from fastapi import FastAPI, Request
    import httpx
    from app.api import system
    from app.core import resource_monitor
    collect = AsyncMock(return_value={})
    monkeypatch.setattr(resource_monitor, "_collect_metrics", collect)
    app = FastAPI()
    app.include_router(system.router)
    def member(request: Request):
        request.state.is_admin = False
        return "api_key:member"
    app.dependency_overrides[system.require_auth] = member
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/diagnostics?whole_stack=true")).status_code == 403
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_counts_follow_broker_lifecycle_without_starting_actor(monkeypatch):
    from app.core import browser_control, browser_runtime, resource_monitor
    monkeypatch.setattr(browser_control, "_brokers", {})
    monkeypatch.setattr(browser_runtime, "_runtimes", {})
    broker = browser_control.get("private-session-id")
    broker.attach("private-viewer-id", AsyncMock())
    before = await resource_monitor._collect_metrics()
    assert before["browser_actors"] == 0
    assert before["browser_viewers"] == 1
    assert before["browser_tabs_reported"] == 0
    browser_control.clear("private-session-id")
    after = await resource_monitor._collect_metrics()
    assert after["browser_viewers"] == 0
    assert not browser_runtime._runtimes
    assert "private-session-id" not in str(after)


def test_process_scope_cache_and_cpu_accounting(monkeypatch):
    from app.core import process_metrics as module
    import psutil
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.now, thread_time=lambda: 0.0))
    monkeypatch.setattr(module, "_CACHE", {})
    monkeypatch.setattr(module, "_PREVIOUS", {})
    monkeypatch.setattr(module, "_COLLECTOR_CPU_SECONDS", 0.0)
    monkeypatch.setattr(module, "_managed_database_process", lambda psutil: None)
    class Process:
        def __init__(self, pid, name, children=()):
            self.pid, self._name, self._children = pid, name, list(children)
        def name(self): return self._name
        def children(self, recursive=False): return self._children
        def parents(self): return [desktop, outer_desktop]
        def create_time(self): return 1.0
        def cmdline(self): return []
        def memory_info(self): return SimpleNamespace(rss=2**20, private=2**19)
        def memory_full_info(self): return SimpleNamespace(uss=2**18)
        def num_threads(self): return 2
        def cpu_times(self): return SimpleNamespace(user=clock.now, system=0)
        def oneshot(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
    backend = Process(module.os.getpid(), "python.exe")
    launcher = Process(91005, "python.exe")
    backend.parents = lambda: [launcher, desktop, outer_desktop]
    database = Process(91001, "postgres.exe")
    desktop = Process(91002, "electron.exe", [launcher, backend, database])
    outer_desktop = Process(91003, "electron.exe", [desktop, Process(91004, "msedge.exe")])
    monkeypatch.setattr(psutil, "Process", lambda pid: backend)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    first = module.collect_process_metrics(True)
    assert first["scope"] == "desktop_tree"
    assert first["cpu_unmeasured_processes"] == 4
    assert {p["pid"] for p in first["processes"]} == {launcher.pid, backend.pid, desktop.pid, database.pid}
    assert first["groups"]["python_launcher"]["processes"] == 1
    assert first["groups"]["database"]["cpu_pct_machine"] is None
    assert module.collect_process_metrics(True) is first
    clock.now += 5
    second = module.collect_process_metrics(True)
    assert second["groups"]["database"]["cpu_pct_machine"] == 25
    assert second["groups"]["database"]["private_resident_mib"] == .25
    assert second["groups"]["database"]["private_commit_mib"] == .5
    assert module.collect_process_metrics()["scope"] == "backend_tree"

@pytest.mark.parametrize("layout", ["container", "preview", "installed_desktop"])
def test_managed_database_attribution_rejects_stale_or_foreign_pid(tmp_path, monkeypatch, layout):
    import psutil
    from app.core import config, process_metrics as module
    cluster_root = tmp_path / "desktop-user-data" if layout == "installed_desktop" else tmp_path
    data = cluster_root / "postgres" / "data"
    data.mkdir(parents=True)
    pidfile = data / "postmaster.pid"
    data_root = tmp_path if layout == "container" else tmp_path / "stable-app-data"
    monkeypatch.delenv("AI_SHUFFLE_ENV_FILE", raising=False)
    if layout == "installed_desktop":
        monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", str(cluster_root / "worker" / ".env"))
    monkeypatch.setattr(config, "settings", SimpleNamespace(effective_data_dir=data_root, get_database_connection=lambda: {"port": 5432}))
    process = SimpleNamespace(name=lambda: "postgres.exe", create_time=lambda: 1000)
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    pidfile.write_text(f"42\n{data}\n1000\n5432\n", encoding="utf-8")
    assert module._managed_database_process(psutil) is process
    pidfile.write_text(f"42\n{data}\n1000\n5433\n", encoding="utf-8")
    assert module._managed_database_process(psutil) is None
    pidfile.write_text(f"42\n{data}\n1\n5432\n", encoding="utf-8")
    assert module._managed_database_process(psutil) is None
    pidfile.write_text(f"42\n{tmp_path}\n1000\n5432\n", encoding="utf-8")
    assert module._managed_database_process(psutil) is None

@pytest.mark.asyncio
async def test_whole_stack_diagnostics_is_opt_in_and_runs_off_loop(monkeypatch):
    import threading
    from fastapi import FastAPI
    import httpx
    from app.api import system
    from app.core import resource_monitor, process_metrics, fluid_memory
    main_thread = threading.get_ident()
    calls = []
    async def metrics():
        return {"streaming_sessions": 0}
    def collect(enabled):
        assert threading.get_ident() != main_thread
        calls.append(enabled)
        return {"scope": "desktop_tree", "groups": {}}
    monkeypatch.setattr(resource_monitor, "_collect_metrics", metrics)
    monkeypatch.setattr(resource_monitor, "_check_warnings", lambda _: [])
    monkeypatch.setattr(process_metrics, "collect_process_metrics", collect)
    journal_calls = []
    def journal():
        assert threading.get_ident() != main_thread
        journal_calls.append(True)
        return {"scope": "configured_worker_stores"}
    monkeypatch.setattr(fluid_memory, "journal_diagnostics", journal)
    from app.db import runs as runs_db
    activity_calls = []
    def activity(local_runner_id):
        assert threading.get_ident() != main_thread
        activity_calls.append(local_runner_id)
        return {"scope": "shared_database", "other_active_runs": 1}
    monkeypatch.setattr(runs_db, "activity_summary", activity)
    app = FastAPI()
    app.include_router(system.router)
    from fastapi import Request
    def owner(request: Request):
        request.state.is_admin = True
        return "test"
    app.dependency_overrides[system.require_auth] = owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        plain = await client.get("/api/v1/diagnostics")
        assert plain.status_code == 200 and "whole_stack" not in plain.json()["metrics"]
        assert not calls and not journal_calls and not activity_calls
        assert "inference_journal" not in plain.json()["metrics"]
        explicit = await client.get("/api/v1/diagnostics?whole_stack=true")
        assert explicit.status_code == 200
        assert explicit.json()["metrics"]["whole_stack"]["scope"] == "desktop_tree"
        assert calls == [True] and journal_calls == [True]
        assert explicit.json()["metrics"]["inference_journal"]["scope"] == "configured_worker_stores"
        scope = explicit.json()["metrics"]["activity_scope"]
        assert len(activity_calls) == 1
        # A sibling worker's leased run makes the snapshot active, never idle.
        assert scope["cross_worker_activity_known"] is True
        assert scope["cross_worker_activity"] is True
        assert scope["activity_label"] == "active"
        assert scope["whole_stack_idle_classified"] is False


@pytest.mark.parametrize(
    ("local", "shared", "label", "cross"),
    [
        ({}, {"unavailable": "OperationalError"}, "unknown", None),
        ({"pool_streaming": 1}, {"unavailable": "OperationalError"}, "active", None),
        ({}, {"scope": "shared_database", "other_active_runs": 0, "stale_live_turns": 3},
         "no_known_session_activity", False),
        ({}, {"scope": "shared_database", "local_live_turns": 1}, "active", False),
        ({"browser_active_calls": 1}, {"scope": "shared_database"}, "active", False),
        ({}, {"scope": "shared_database", "other_live_turns": 1}, "active", True),
        # Background tasks have no lease: counted as activity even unattributed.
        ({}, {"scope": "shared_database", "unattributed_running_background_tasks": 1}, "active", True),
    ],
)
def test_whole_stack_activity_label_is_only_idle_when_every_signal_was_read(local, shared, label, cross):
    from app.api.system import whole_stack_activity_scope
    scope = whole_stack_activity_scope(local, shared)
    assert scope["activity_label"] == label
    assert scope["cross_worker_activity"] is cross
    assert scope["cross_worker_activity_known"] is ("unavailable" not in shared)
    assert scope["whole_stack_idle_classified"] is (label == "no_known_session_activity")
    assert scope["shared_database"] is shared


def test_journal_coverage_drives_append_cursor_reap_without_exposing_contents(tmp_path, monkeypatch):
    import json
    from app.core import fluid_memory
    from fluid.codemode import skills
    root = tmp_path / "skills"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    monkeypatch.setattr(skills, "JOURNAL_KEEP_ENTRIES", 1)
    before = skills.journal_metrics()
    store = skills.SkillStore(root)
    for _ in range(6):
        store.journal_append({"kind": "inference", "content": "never-collect-this-transcript"})
    store.set_synth_cursor(3)
    observed = fluid_memory.journal_diagnostics()
    assert observed["coverage_complete"]
    assert observed["retained_entries_known"] == 6
    assert observed["consumed_entries_known"] == 3
    assert observed["pending_entries_known"] == 3
    assert observed["process_events"]["appended_entries"] - before["appended_entries"] == 6
    assert store.reap_journal() == 3
    after = fluid_memory.journal_diagnostics()
    assert after["retained_entries_known"] == 3
    assert after["consumed_entries_known"] == 0
    assert after["pending_entries_known"] == 3
    assert after["retained_bytes"] < observed["retained_bytes"]
    assert after["process_events"]["reaped_entries"] - before["reaped_entries"] == 3
    assert "never-collect-this-transcript" not in json.dumps(after)
    assert str(root) not in json.dumps(after)


def test_journal_diagnostics_missing_store_is_read_only(tmp_path, monkeypatch):
    from app.core import fluid_memory
    root = tmp_path / "absent"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    result = fluid_memory.journal_diagnostics()
    assert result["coverage_complete"] and result["journals_found"] == 0
    assert not root.exists()


def test_journal_diagnostics_marks_byte_and_discovery_caps_incomplete(tmp_path, monkeypatch):
    from app.core import fluid_memory
    from fluid.codemode.skills import SkillStore
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: tmp_path)
    monkeypatch.setattr(fluid_memory, "JOURNAL_DIAGNOSTIC_BYTES", 16)
    monkeypatch.setattr(fluid_memory, "JOURNAL_DIAGNOSTIC_STORES", 1)
    SkillStore(tmp_path).journal_append({"content": "a" * 100})
    other = SkillStore(tmp_path / ".users" / ("a" * 64))
    other.journal_append({"content": "not scanned"})
    result = fluid_memory.journal_diagnostics()
    assert result["bytes_scanned"] == 16
    assert not result["coverage_complete"]
    assert result["journals_incomplete"] == 1
    assert result["discovery_incomplete"]
    assert result["retained_entries_known"] == 0  # unknown is never an exact zero
    assert result["stores_examined"] == 1


def test_journal_diagnostics_aggregates_owner_stores_and_rejects_bad_cursor(tmp_path, monkeypatch):
    from app.core import fluid_memory
    from fluid.codemode.skills import SkillStore
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: tmp_path)
    local = SkillStore(tmp_path)
    user = SkillStore(tmp_path / ".users" / ("a" * 64))
    local.journal_append({"content": "local"})
    user.journal_append({"content": "private user"})
    result = fluid_memory.journal_diagnostics()
    assert result["coverage_complete"] and result["journals_found"] == 2
    assert result["pending_entries_known"] == 2
    (user.root / "meta.json").write_text('{"synth_cursor": "not-an-integer"}')
    result = fluid_memory.journal_diagnostics()
    assert not result["coverage_complete"] and result["journals_incomplete"] == 1
    assert result["retained_entries_known"] == 2
    assert result["pending_entries_known"] == 1


def test_journal_diagnostics_caps_metadata_without_inventing_pending_count(tmp_path, monkeypatch):
    from app.core import fluid_memory
    from fluid.codemode.skills import SkillStore
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: tmp_path)
    monkeypatch.setattr(fluid_memory, "JOURNAL_DIAGNOSTIC_META_BYTES", 4)
    store = SkillStore(tmp_path)
    store.journal_append({"content": "fixture"})
    store.set_synth_cursor(1)
    result = fluid_memory.journal_diagnostics()
    assert result["retained_entries_known"] == 1
    assert result["journals_incomplete"] == 1
    assert result["pending_entries_known"] == 0 and not result["coverage_complete"]


def test_journal_diagnostics_rejects_links_and_special_files(tmp_path, monkeypatch):
    import os
    from app.core import fluid_memory
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: tmp_path)
    target = tmp_path / "private.txt"
    target.write_text("private transcript")
    journal = tmp_path / "journal.jsonl"
    try:
        journal.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = fluid_memory.journal_diagnostics()
    assert result["journals_incomplete"] == 1 and result["bytes_scanned"] == 0
    if hasattr(os, "mkfifo"):
        journal.unlink()
        os.mkfifo(journal)
        result = fluid_memory.journal_diagnostics()
        assert result["journals_incomplete"] == 1 and result["bytes_scanned"] == 0


def test_journal_diagnostics_counts_silent_learners_without_naming_them(tmp_path, monkeypatch):
    import json
    import time
    from app.core import fluid_memory
    from fluid.codemode.skills import SkillStore
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: tmp_path)
    local = SkillStore(tmp_path)
    user = SkillStore(tmp_path / ".users" / ("a" * 64))
    idle = SkillStore(tmp_path / ".users" / ("b" * 64))
    for store in (local, user, idle):
        store.journal_append({"content": "fixture"})
    # Passes for four days, none of them writing.
    local.note_learner_pass(False, now=time.time() - 4 * 86400)
    local.note_learner_pass(False, now=time.time() - 60)
    user.note_learner_pass(False, now=time.time() - 3600)  # silent for an hour
    # One silent pass four days ago and none since: the learner stopped
    # running, which is not three days of passes without a write.
    idle.note_learner_pass(False, now=time.time() - 4 * 86400)
    result = fluid_memory.journal_diagnostics()
    assert result["coverage_complete"]
    assert result["learner_silent_stores"] == 1
    assert result["learner_silence_alarm_seconds"] == 3 * 86400
    local.note_learner_pass(True, now=time.time())
    assert fluid_memory.journal_diagnostics()["learner_silent_stores"] == 0
    assert str(tmp_path) not in json.dumps(result)


@pytest.fixture
def fresh_loss_buffer(monkeypatch):
    from collections import OrderedDict

    from fluid.codemode import skills
    monkeypatch.setattr(skills, "_JOURNAL_UNPERSISTED", OrderedDict())
    monkeypatch.setattr(skills, "_JOURNAL_BACKOFF", OrderedDict())
    return skills


def test_saturated_journal_loss_history_survives_restart_rate_limited(
        tmp_path, monkeypatch, fresh_loss_buffer):
    import json
    from collections import OrderedDict

    from app.core import fluid_memory
    skills = fresh_loss_buffer
    root = tmp_path / "skills"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    store = skills.SkillStore(root)
    monkeypatch.setattr(skills, "JOURNAL_MAX_BYTES", 1)
    monkeypatch.setattr(store, "reap_journal", lambda: 0)
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(skills, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    writes = []
    real_write = skills.atomic_write_text
    monkeypatch.setattr(skills, "atomic_write_text", lambda p, t: (writes.append(p.name), real_write(p, t)))
    for _ in range(120):
        store.journal_append({"kind": "inference", "content": "never-collect-this-transcript"})
    # The first skip opens the warning window and is recorded at once; the
    # rest stay buffered instead of rewriting the record per dropped entry.
    assert writes == ["journal_loss.json"]
    assert skills.journal_metrics()["loss_history_pending"] == 119
    clock.now += 61
    store.journal_append({"kind": "inference", "content": "x"})
    assert writes == ["journal_loss.json"] * 2
    with pytest.raises(OSError, match="quota"):
        store.journal_append({"kind": "remember", "content": "explicit"})
    assert skills.journal_metrics()["loss_history_pending"] == 0

    # A new process starts with empty counters; the store still knows.
    monkeypatch.setattr(skills, "_JOURNAL_UNPERSISTED", OrderedDict())
    result = fluid_memory.journal_diagnostics()
    history = result["loss_history"]
    assert history["stores_with_history"] == 1
    assert history["skipped_quota"] == 121
    assert history["explicit_quota_failures"] == 1
    assert history["first_at"] == 1_000.0 and history["last_quota_at"] == 1_061.0
    import time as real_time
    monkeypatch.setattr(real_time, "time", lambda: 1_121.0)  # the diagnostics' wall clock
    assert fluid_memory.journal_diagnostics()["loss_history"]["saturation_observed_last_hour"] is True
    monkeypatch.setattr(real_time, "time", lambda: 4_721.0)
    later = fluid_memory.journal_diagnostics()["loss_history"]
    monkeypatch.undo()
    assert later["saturation_observed_last_hour"] is False
    assert later["last_quota_age_seconds"] == 3660.0
    rendered = json.dumps(result)
    assert "never-collect-this-transcript" not in rendered and str(root) not in rendered
    record = json.loads((root / "journal_loss.json").read_text(encoding="utf-8"))
    assert set(record) == {
        "schema", "skipped_quota", "skipped_busy", "explicit_quota_failures",
        "explicit_busy_failures", "dropped_unsynthesized", "first_at", "last_at", "last_quota_at",
    }


def test_journal_loss_stays_buffered_while_record_is_busy_then_flushes(tmp_path, fresh_loss_buffer):
    from fluid._filelock import file_lock
    skills = fresh_loss_buffer
    store = skills.SkillStore(tmp_path / "skills")
    record = store.root / "journal_loss.json"
    with file_lock(record.with_name(record.name + ".lock")) as held:
        assert held
        store._journal_skip("journal is busy")
        assert not record.exists()
        assert skills.journal_metrics()["loss_history_pending"] == 1
    store._journal_skip("journal is busy")  # inside the window: buffered only
    assert not record.exists()
    assert skills.flush_journal_loss() == 0
    assert skills.read_journal_loss(record)["skipped_busy"] == 2
    assert skills.journal_metrics()["loss_history_pending"] == 0


def test_journal_loss_record_rejects_corrupt_special_and_oversized_files(tmp_path, fresh_loss_buffer):
    import os
    skills = fresh_loss_buffer
    store = skills.SkillStore(tmp_path / "skills")
    record = store.root / "journal_loss.json"
    record.write_text('{"skipped_quota": "lots", "first_at": NaN, "last_at": -4, "extra": "x"}')
    assert skills.read_journal_loss(record) == {
        "schema": 1, "skipped_quota": 0, "skipped_busy": 0, "explicit_quota_failures": 0,
        "explicit_busy_failures": 0, "dropped_unsynthesized": 0,
    }
    record.write_text("[1]")
    assert skills.read_journal_loss(record) is None
    record.write_text(" " * (skills.JOURNAL_LOSS_MAX_BYTES + 1) + "{}")
    assert skills.read_journal_loss(record) is None
    record.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"skipped_quota": 5}')
    record.symlink_to(target)
    assert skills.read_journal_loss(record) is None
    record.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(record)
        assert skills.read_journal_loss(record) is None  # never blocks on a FIFO
        store._journal_skip("journal byte quota reached")
        assert skills.read_journal_loss(record)["skipped_quota"] == 1
    assert target.read_text() == '{"skipped_quota": 5}'


def test_journal_loss_buffer_is_bounded(tmp_path, monkeypatch, fresh_loss_buffer):
    skills = fresh_loss_buffer
    monkeypatch.setattr(skills, "_JOURNAL_LOSS_LIMIT", 2)
    before = skills.journal_metrics()["loss_history_unpersisted"]
    for name in ("a", "b", "c"):
        skills._journal_event("skipped_busy", 3, journal=str(tmp_path / name / "journal.jsonl"))
    assert len(skills._JOURNAL_UNPERSISTED) == 2
    assert skills.journal_metrics()["loss_history_unpersisted"] - before == 3
    assert skills.journal_metrics()["loss_history_pending"] == 6
