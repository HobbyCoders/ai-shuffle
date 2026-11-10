"""A memory push cut off by shutdown is finished by the next start (#1305).

The DB layer is the in-memory ``FakeMemDb`` from test_memory_sync, but its
write is a real blocking call on the real push thread: the stall is in the
transport, not a mock of the push. "Process death" abandons that thread and
then makes its write fail, so the old process never completes the push.
"""
import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest

from app.core import memory_sync
from tests.core.test_memory_sync import FakeMemDb, KEY, T0


class StallableDb(FakeMemDb):
    """Writes block while ``stall`` is set; released writes fail like a dead process."""

    def __init__(self):
        super().__init__()
        self.stall = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.uploads = []

    def upsert_memory_file(self, memory_key, relative_path, content, content_hash,
                           updated_by=None, updated_at=None):
        if self.stall:
            self.entered.set()
            assert self.release.wait(10)
            raise ConnectionError("process killed while the write was in flight")
        self.uploads.append((relative_path, content_hash))
        super().upsert_memory_file(memory_key, relative_path, content, content_hash,
                                   updated_by, updated_at)


@pytest.fixture
def target(tmp_path, monkeypatch):
    folder = tmp_path / "memory"
    folder.mkdir()
    fake = StallableDb()
    config = SimpleNamespace(agent_memory_sync=True, effective_instance_id="test-instance",
                             effective_data_dir=tmp_path / "data")
    projects = {"project": {"id": "project"}}
    monkeypatch.setattr(memory_sync, "memdb", fake)
    monkeypatch.setattr(memory_sync, "settings", config)
    monkeypatch.setattr(memory_sync, "database", SimpleNamespace(get_project=projects.get))
    monkeypatch.setattr(memory_sync, "_resolve_target", lambda *args: (KEY, folder))
    memory_sync.reset_state()
    yield SimpleNamespace(folder=folder, db=fake, config=config, projects=projects)
    fake.release.set()  # Let a still-stalled thread fail and exit.
    memory_sync.reset_state()


def _write(folder, name, text, offset=60):
    path = folder / name
    path.write_text(text)
    os.utime(path, (T0.timestamp() + offset,) * 2)
    return path


async def _stop_during_stalled_push(env, push, *, stall=True):
    """Run ``push`` until its DB write stalls, then end the process under it."""
    env.db.stall = stall
    pushing = asyncio.create_task(push())
    assert await asyncio.to_thread(env.db.entered.wait, 5)
    pushing.cancel()  # The absolute stop deadline ends the awaiter ...
    with pytest.raises(asyncio.CancelledError):
        await pushing
    # ... and the process exits: nothing of this process's memory survives.
    memory_sync.reset_state()
    env.db.stall = False
    env.db.entered.clear()
    # Its in-flight write never lands: the abandoned thread fails and ends.
    dead, env.db.release = env.db.release, threading.Event()
    dead.set()


def _markers(env):
    folder = env.config.effective_data_dir / "memory-sync-pending"
    return sorted(folder.glob("*.json")) if folder.exists() else []


async def test_stalled_turn_end_push_is_published_once_by_next_start(target):
    env = target
    local = _write(env.folder, "MEMORY.md", "fact written before the stop")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    assert env.db.get_memory_file(KEY, "MEMORY.md") is None  # Unpublished at exit.

    await memory_sync.publish_pending_memory()  # Next start, no turn in the project.

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == local.read_text()
    assert [path for path, _ in env.db.uploads] == ["MEMORY.md"]
    assert _markers(env) == []
    await memory_sync.publish_pending_memory()  # A later start has nothing owed.
    assert len(env.db.uploads) == 1
    assert local.read_text() == "fact written before the stop"


async def test_kill_during_startup_retry_retries_once_per_file_version(target):
    env = target
    _write(env.folder, "MEMORY.md", "first file")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    # The retry publishes one file, then is killed while writing the second.
    _write(env.folder, "topic.md", "second file", offset=61)
    original = env.db.upsert_memory_file

    def stall_after_first(*args, **kwargs):
        if env.db.uploads:
            env.db.stall = True
        return original(*args, **kwargs)

    env.db.upsert_memory_file = stall_after_first
    await _stop_during_stalled_push(env, memory_sync.publish_pending_memory, stall=False)
    del env.db.upsert_memory_file
    assert len(_markers(env)) == 1  # Still owed after the second death.

    await memory_sync.publish_pending_memory()
    assert sorted(path for path, _ in env.db.uploads) == ["MEMORY.md", "topic.md"]
    assert env.db.get_memory_file(KEY, "topic.md")["content"] == "second file"
    assert _markers(env) == []


async def test_newer_peer_copy_wins_and_local_file_is_kept(target):
    env = target
    local = _write(env.folder, "MEMORY.md", "local copy", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    env.db.seed_live("MEMORY.md", "newer peer copy", T0.replace(hour=13))

    await memory_sync.publish_pending_memory()

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == "newer peer copy"
    assert local.read_text() == "local copy"  # The retry never writes local files.
    assert _markers(env) == []  # Last-write-wins settled it; nothing still owed.


async def test_older_peer_copy_is_replaced_by_newer_local_file(target):
    env = target
    env.db.seed_live("MEMORY.md", "older shared copy", T0)
    local = _write(env.folder, "MEMORY.md", "newer local copy", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))

    await memory_sync.publish_pending_memory()

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == local.read_text()


async def test_start_without_queue_behaves_as_before(target):
    env = target
    _write(env.folder, "MEMORY.md", "pre-upgrade local file")
    await memory_sync.publish_pending_memory()
    assert env.db.uploads == []  # No marker: the next native turn reconciles as today.


async def test_disabled_sync_pushes_nothing_and_keeps_the_queue(target):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    env.config.agent_memory_sync = False
    await memory_sync.publish_pending_memory()
    assert env.db.uploads == [] and len(_markers(env)) == 1
    env.config.agent_memory_sync = True
    await memory_sync.publish_pending_memory()
    assert len(env.db.uploads) == 1 and _markers(env) == []


@pytest.mark.parametrize("gone", ["project", "memory folder"])
async def test_deleted_project_or_folder_pushes_nothing_and_clears(target, gone):
    env = target
    local = _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    if gone == "project":
        env.projects.clear()
    else:
        local.unlink()
        env.folder.rmdir()
    await memory_sync.publish_pending_memory()
    assert env.db.uploads == [] and _markers(env) == []


async def test_unreachable_database_keeps_the_queue_for_the_next_start(target, monkeypatch):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))

    def unavailable(project_id):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(memory_sync, "database", SimpleNamespace(get_project=unavailable))
    await memory_sync.publish_pending_memory()
    assert env.db.uploads == [] and len(_markers(env)) == 1


async def test_queue_is_one_marker_per_project_and_unreadable_markers_are_dropped(target):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    for _ in range(3):
        await _stop_during_stalled_push(
            env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    markers = _markers(env)
    assert len(markers) == 1
    assert json.loads(markers[0].read_text())["project_id"] == "project"
    (markers[0].parent / "garbage.json").write_text("{not json")
    await memory_sync.publish_pending_memory()
    assert _markers(env) == [] and len(env.db.uploads) == 1


async def test_delegated_leg_and_completed_pushes_leave_no_queue(target):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    await memory_sync.push_memory("project", str(env.folder), "delegated")
    assert env.db.uploads == [] and _markers(env) == []
    await memory_sync.push_memory("project", str(env.folder), "native")
    assert len(env.db.uploads) == 1 and _markers(env) == []
    assert memory_sync.pending_publications() == 0


async def test_in_flight_publication_is_reported_until_it_ends(target):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    env.db.stall = True
    pushing = asyncio.create_task(memory_sync.push_memory("project", str(env.folder), "native"))
    assert await asyncio.to_thread(env.db.entered.wait, 5)
    assert memory_sync.pending_publications() == 1
    env.db.release.set()  # The write fails; the marker stays for the next start.
    await pushing
    assert memory_sync.pending_publications() == 0
    assert len(_markers(env)) == 1
