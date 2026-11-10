"""A memory push cut off by shutdown is finished by the next start (#1305).

The DB layer is the real ``app.db.agent_memory`` SQL on the per-test database
(conftest's ``_auto_db_shim``), wrapped so a write is a real blocking call on
the real push thread: the stall is in the transport, not a mock of the push.
"Process death" abandons that thread and then makes its write fail, so the old
process never completes the push.
"""
import asyncio
import json
import os
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core import memory_sync
from app.db import agent_memory
from tests.core.test_memory_sync import KEY, T0

_real_resolve_target = memory_sync._resolve_target


class StallableDb:
    """Real SQL; writes block while ``stall`` is set and released writes fail
    like a dead process. ``before_write`` runs once, just before the next write."""

    get_memory_files = staticmethod(agent_memory.get_memory_files)
    get_memory_file = staticmethod(agent_memory.get_memory_file)
    tombstone_memory_file = staticmethod(agent_memory.tombstone_memory_file)

    def __init__(self):
        self.stall = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.uploads = []
        self.before_write = None

    def _gate(self):
        hook, self.before_write = self.before_write, None
        if hook is not None:
            hook()
        if self.stall:
            self.entered.set()
            assert self.release.wait(10)
            raise ConnectionError("process killed while the write was in flight")

    def upsert_memory_file(self, memory_key, relative_path, content, content_hash,
                           updated_by=None, updated_at=None):
        self._gate()
        self.uploads.append((relative_path, content_hash))
        agent_memory.upsert_memory_file(memory_key, relative_path, content, content_hash,
                                        updated_by, updated_at)

    def upsert_memory_file_if_not_newer(self, memory_key, relative_path, content,
                                        content_hash, updated_by, updated_at):
        self._gate()
        wrote = agent_memory.upsert_memory_file_if_not_newer(
            memory_key, relative_path, content, content_hash, updated_by, updated_at)
        if wrote:
            self.uploads.append((relative_path, content_hash))
        return wrote

    def seed_live(self, rel, content, updated_at):
        agent_memory.upsert_memory_file(KEY, rel, content, memory_sync._sha256(content),
                                        "remote", updated_at.isoformat())

    def seed_tombstone(self, rel, updated_at):
        agent_memory.tombstone_memory_file(KEY, rel, "remote", updated_at.isoformat())


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
    monkeypatch.setattr(memory_sync, "_resolve_target", lambda *args, **kwargs: (KEY, folder))
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
    original = env.db.upsert_memory_file_if_not_newer

    def stall_after_first(*args, **kwargs):
        if env.db.uploads:
            env.db.stall = True
        return original(*args, **kwargs)

    env.db.upsert_memory_file_if_not_newer = stall_after_first
    await _stop_during_stalled_push(env, memory_sync.publish_pending_memory, stall=False)
    del env.db.upsert_memory_file_if_not_newer
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


# --- The replay publishes an old edit late (S1, S2) --------------------------
# A real turn reconciles at its start and pushes at its end; the replay must
# neither outrank that turn's newer edit nor revive a newer peer delete.

def _turn(env, name, text, offset):
    memory_sync.reconcile_memory_sync("project", str(env.folder), "native")
    _write(env.folder, name, text, offset)
    memory_sync.push_memory_sync("project", str(env.folder), "native")


def _stamp(env, name):
    return memory_sync._as_dt(env.db.get_memory_file(KEY, name)["updated_at"])


async def test_replay_delayed_past_a_newer_turn_edit_does_not_replace_it(target):
    env = target
    _write(env.folder, "MEMORY.md", "old fact", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    # The replay has scanned the old file; before its write lands, a turn edits
    # the file and publishes the edit.
    env.db.before_write = lambda: _turn(env, "MEMORY.md", "newer fact", offset=120)

    await memory_sync.publish_pending_memory()

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == "newer fact"
    memory_sync.reconcile_memory_sync("project", str(env.folder), "native")
    assert (env.folder / "MEMORY.md").read_text() == "newer fact"
    assert _markers(env) == []


async def test_replay_landing_first_does_not_outrank_the_later_turn_edit(target):
    env = target
    _write(env.folder, "MEMORY.md", "old fact", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))

    await memory_sync.publish_pending_memory()
    # The late upload keeps the file's own time, not the time it was replayed.
    assert _stamp(env, "MEMORY.md") == T0 + timedelta(seconds=60)
    _turn(env, "MEMORY.md", "newer fact", offset=120)

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == "newer fact"
    memory_sync.reconcile_memory_sync("project", str(env.folder), "native")  # Next turn.
    assert (env.folder / "MEMORY.md").read_text() == "newer fact"


async def test_replay_does_not_revive_a_newer_peer_delete(target):
    env = target
    _write(env.folder, "topic.md", "old fact", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    env.db.seed_tombstone("topic.md", T0 + timedelta(seconds=120))

    await memory_sync.publish_pending_memory()

    row = env.db.get_memory_file(KEY, "topic.md")
    assert row["deleted"] and _stamp(env, "topic.md") == T0 + timedelta(seconds=120)
    assert env.db.uploads == [] and _markers(env) == []
    assert (env.folder / "topic.md").read_text() == "old fact"  # Never writes local files.


async def test_replay_revives_an_older_peer_delete(target):
    env = target
    env.db.seed_tombstone("topic.md", T0)
    _write(env.folder, "topic.md", "re-created fact", offset=60)
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))

    await memory_sync.publish_pending_memory()

    row = env.db.get_memory_file(KEY, "topic.md")
    assert not row["deleted"] and row["content"] == "re-created fact"
    assert _stamp(env, "topic.md") == T0 + timedelta(seconds=60)


@pytest.mark.parametrize("deleted", [False, True])
def test_conditional_write_never_moves_a_stamp_backwards(deleted):
    """The real statement: replace a live row or tombstone only when not newer."""
    newer, older = T0 + timedelta(seconds=120), T0 + timedelta(seconds=60)
    if deleted:
        agent_memory.tombstone_memory_file(KEY, "f.md", "peer", newer.isoformat())
    else:
        agent_memory.upsert_memory_file(KEY, "f.md", "peer", "h-peer", "peer", newer.isoformat())

    assert not agent_memory.upsert_memory_file_if_not_newer(
        KEY, "f.md", "late", "h-late", "replay", older.isoformat())
    row = agent_memory.get_memory_file(KEY, "f.md")
    assert bool(row["deleted"]) is deleted and memory_sync._as_dt(row["updated_at"]) == newer

    assert agent_memory.upsert_memory_file_if_not_newer(
        KEY, "f.md", "same time", "h-same", "replay", newer.isoformat())
    assert agent_memory.upsert_memory_file_if_not_newer(
        KEY, "g.md", "new file", "h-new", "replay", older.isoformat())
    rows = {row["relative_path"]: row for row in agent_memory.get_memory_files(KEY)}
    assert rows["f.md"]["content"] == "same time" and not rows["f.md"]["deleted"]
    assert memory_sync._as_dt(rows["g.md"]["updated_at"]) == older


# --- A failed project lookup is not "nothing to publish" (S3) ----------------
# Through the real _resolve_target: only the folder location is pinned.

@pytest.fixture
def real_lookup(target, monkeypatch):
    env = target
    env.projects["project"] = {"id": "project", "path": str(env.folder), "link_group_id": KEY}
    env.lookups = []

    def get_project(project_id):
        env.lookups.append(project_id)
        if env.lookup_fails(len(env.lookups)):
            raise ConnectionError("database unavailable")
        return env.projects.get(project_id)

    env.lookup_fails = lambda attempt: False
    monkeypatch.setattr(memory_sync, "_resolve_target", _real_resolve_target)
    monkeypatch.setattr(memory_sync, "memory_dir_for_working_dir", lambda anchor, leg: env.folder)
    monkeypatch.setattr(memory_sync, "database", SimpleNamespace(get_project=get_project))
    return env


async def test_lookup_failure_in_turn_end_push_keeps_the_marker(real_lookup):
    env = real_lookup
    _write(env.folder, "MEMORY.md", "fact")
    env.lookup_fails = lambda attempt: True

    await memory_sync.push_memory("project", str(env.folder), "native")

    assert env.db.uploads == [] and len(_markers(env)) == 1
    assert memory_sync.pending_publications() == 0
    env.lookup_fails = lambda attempt: False
    await memory_sync.publish_pending_memory()  # The next start publishes it.
    assert len(env.db.uploads) == 1 and _markers(env) == []


async def test_replay_does_not_depend_on_a_second_project_lookup(real_lookup):
    env = real_lookup
    _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    env.lookups.clear()
    env.lookup_fails = lambda attempt: attempt > 1  # The database drops after one read.

    await memory_sync.publish_pending_memory()

    assert env.db.get_memory_file(KEY, "MEMORY.md")["content"] == "fact"
    assert env.lookups == ["project"] and _markers(env) == []


async def test_replay_lookup_failure_keeps_the_marker(real_lookup):
    env = real_lookup
    _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))
    env.lookup_fails = lambda attempt: True

    await memory_sync.publish_pending_memory()

    assert env.db.uploads == [] and len(_markers(env)) == 1


# --- The replay is unfinished saving until it has ended (S5) -----------------

async def _queued_marker(env):
    _write(env.folder, "MEMORY.md", "fact")
    await _stop_during_stalled_push(
        env, lambda: memory_sync.push_memory("project", str(env.folder), "native"))


@pytest.fixture
def blocking_lookup(target, monkeypatch):
    """The replay's project lookup blocks until ``release`` is set."""
    env = target
    env.looking_up, env.lookup_release = threading.Event(), threading.Event()

    def get_project(project_id):
        env.looking_up.set()
        assert env.lookup_release.wait(10)
        return env.projects.get(project_id)

    monkeypatch.setattr(memory_sync, "database", SimpleNamespace(get_project=get_project))
    yield env
    env.lookup_release.set()


async def test_replay_is_unfinished_while_it_resolves_its_project(blocking_lookup):
    env = blocking_lookup
    await _queued_marker(env)
    replay = asyncio.create_task(memory_sync.publish_pending_memory())
    assert await asyncio.to_thread(env.looking_up.wait, 5)

    assert memory_sync.pending_publications() == 1
    env.lookup_release.set()
    await replay
    assert memory_sync.pending_publications() == 0 and _markers(env) == []


async def test_replay_thread_outliving_its_cancelled_awaiter_stays_counted(blocking_lookup):
    env = blocking_lookup
    await _queued_marker(env)
    replay = asyncio.create_task(memory_sync.publish_pending_memory())
    assert await asyncio.to_thread(env.looking_up.wait, 5)
    replay.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replay

    assert memory_sync.pending_publications() == 1  # The thread is still running.
    env.lookup_release.set()
    for _ in range(500):
        if memory_sync.pending_publications() == 0:
            break
        await asyncio.sleep(0.01)
    assert memory_sync.pending_publications() == 0 and _markers(env) == []


@pytest.fixture
async def one_busy_thread():
    """The loop's only worker thread is busy, so the next dispatch waits in line."""
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    busy, release = threading.Event(), threading.Event()
    occupant = loop.run_in_executor(None, lambda: (busy.set(), release.wait(10)))
    assert busy.wait(5)
    yield release
    release.set()
    await occupant


@pytest.mark.parametrize("which", ["replay", "turn-end push"])
async def test_cancelled_while_queued_for_a_thread_is_not_counted_forever(
        target, one_busy_thread, which):
    env = target
    _write(env.folder, "MEMORY.md", "fact")
    if which == "replay":
        memory_sync._queue_publication("project", str(env.folder), "native")
        memory_sync.reset_state()  # Left by a previous process.
        start = memory_sync.publish_pending_memory
    else:
        def start():
            return memory_sync.push_memory("project", str(env.folder), "native")
    waiting = asyncio.create_task(start())
    await asyncio.sleep(0.05)

    assert memory_sync.pending_publications() == 1  # Queued for a thread: still owed.
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert memory_sync.pending_publications() == 0
    assert env.db.uploads == [] and len(_markers(env)) == 1  # The next start retries it.
