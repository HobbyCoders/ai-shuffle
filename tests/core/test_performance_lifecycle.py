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
    send.assert_awaited_with("user-7", {"type": "chat_access_changed", "session_id": "session-7"})
    request_id = next(iter(broker.requests))
    await broker.decline("session-7", request_id)
    with pytest.raises(chat_access.AccessDenied, match="declined"):
        await task
    await asyncio.gather(*broker._notifications)
    assert send.await_count == 2
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


def test_process_scope_cache_and_cpu_accounting(monkeypatch):
    from app.core import process_metrics as module
    import psutil
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_CACHE", {})
    monkeypatch.setattr(module, "_PREVIOUS", {})
    monkeypatch.setattr(module, "_managed_database_process", lambda psutil: None)
    class Process:
        def __init__(self, pid, name, children=()):
            self.pid, self._name, self._children = pid, name, list(children)
        def name(self): return self._name
        def children(self, recursive=False): return self._children
        def parents(self): return [desktop]
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
    database = Process(91001, "postgres.exe")
    desktop = Process(91002, "electron.exe", [backend, database])
    monkeypatch.setattr(psutil, "Process", lambda pid: backend)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    first = module.collect_process_metrics(True)
    assert first["scope"] == "desktop_tree"
    assert first["cpu_unmeasured_processes"] == 3
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
    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[system.require_auth] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        plain = await client.get("/api/v1/diagnostics")
        assert plain.status_code == 200 and "whole_stack" not in plain.json()["metrics"]
        assert not calls and not journal_calls
        assert "inference_journal" not in plain.json()["metrics"]
        explicit = await client.get("/api/v1/diagnostics?whole_stack=true")
        assert explicit.status_code == 200
        assert explicit.json()["metrics"]["whole_stack"]["scope"] == "desktop_tree"
        assert calls == [True] and journal_calls == [True]
        assert explicit.json()["metrics"]["inference_journal"]["scope"] == "configured_worker_stores"
        assert explicit.json()["metrics"]["activity_scope"]["whole_stack_idle_classified"] is False


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
