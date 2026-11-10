"""A cancelled legacy memory push misses replication, not the already-written file."""
import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from app.core import memory_sync
from tests.core.test_memory_sync import FakeMemDb, KEY, T0


@pytest.fixture
def target(tmp_path, monkeypatch):
    folder = tmp_path / "memory"
    folder.mkdir()
    fake = FakeMemDb()
    monkeypatch.setattr(memory_sync, "memdb", fake)
    monkeypatch.setattr(memory_sync, "settings", SimpleNamespace(
        agent_memory_sync=True, effective_instance_id="test-instance",
        effective_data_dir=tmp_path / "data",
    ))
    monkeypatch.setattr(memory_sync, "_resolve_target", lambda *args, **kwargs: (KEY, folder))
    memory_sync.reset_state()
    yield folder, fake
    memory_sync.reset_state()


@pytest.mark.parametrize("existing_remote", [False, True])
async def test_cancelled_push_recovers_local_file_on_next_native_turn(target, monkeypatch, existing_remote):
    folder, fake = target
    if existing_remote:
        fake.seed_live("MEMORY.md", "old shared fact", T0)
    local = folder / "MEMORY.md"
    local.write_text("new fact already durable on disk")
    os.utime(local, (T0.timestamp() + 60,) * 2)
    submitted = asyncio.Event()
    real_sync = memory_sync.run_sync

    async def queued_dispatch(*args, **kwargs):
        submitted.set()
        await asyncio.Event().wait()  # Represents work not yet issued by the tail.

    monkeypatch.setattr(memory_sync, "run_sync", queued_dispatch)
    pushing = asyncio.create_task(memory_sync.push_memory("project", str(folder), "native"))
    await submitted.wait()
    pushing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pushing
    assert local.read_text() == "new fact already durable on disk"
    row = fake.get_memory_file(KEY, "MEMORY.md")
    assert row is None or row["content"] == "old shared fact"

    # Process restart drops only merge bookkeeping, never the local files.
    memory_sync.reset_state()
    monkeypatch.setattr(memory_sync, "run_sync", real_sync)
    # These are the real entrypoints used before/after the next qualifying turn,
    # not an assumption that worker startup alone retries a push.
    await memory_sync.reconcile_memory("project", str(folder), "native")
    assert local.read_text() == "new fact already durable on disk"
    await memory_sync.push_memory("project", str(folder), "native")
    assert fake.get_memory_file(KEY, "MEMORY.md")["content"] == local.read_text()


async def test_already_issued_sync_push_can_finish_after_awaiter_cancellation(target, monkeypatch):
    folder, fake = target
    (folder / "MEMORY.md").write_text("durable fact")
    started, release, uploaded = threading.Event(), threading.Event(), threading.Event()
    original_get, original_upsert = fake.get_memory_files, fake.upsert_memory_file

    def delayed_get(key):
        started.set()
        assert release.wait(5)
        return original_get(key)

    def upsert(*args, **kwargs):
        original_upsert(*args, **kwargs)
        uploaded.set()

    monkeypatch.setattr(fake, "get_memory_files", delayed_get)
    monkeypatch.setattr(fake, "upsert_memory_file", upsert)
    pushing = asyncio.create_task(memory_sync.push_memory("project", str(folder), "native"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pushing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pushing
    finally:
        release.set()
    assert await asyncio.to_thread(uploaded.wait, 5)
    assert fake.get_memory_file(KEY, "MEMORY.md")["content"] == "durable fact"
