"""A failed write leaves a store's live state equal to what a reopen sees.

Every JsonlStore mutator commits its record before it publishes anything to
memory. A write that fails may still have landed, whole (fsync or close failed
after the bytes went out) or in part (a short write, disk full mid-line), so
the store then re-reads the session from disk. Each test injects one of those
faults and compares the failing store with a fresh one on the same directory.

Duplicate node ids follow one rule live and on replay: the same node again is
idempotent (only the head moves, as an append always does), a different node
under an existing id is refused before anything is written.
"""

from __future__ import annotations

import json

import pytest

from fluid.events.model import TurnStarted
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store import jsonl
from fluid.store.base import MemoryStore, SessionNode
from fluid.store.jsonl import JsonlStore

SID = "ses_fault"

# "open": nothing written (the file cannot be opened for append).
# "partial": half the line written, then an error (disk full mid-write).
# "landed": the whole line written, then an error (fsync or close failed).
# "newline": every byte but the trailing newline written, then an error; the
# record reads back whole, so the next write must start a new line first.
FAULTS = ("open", "partial", "landed", "newline")
LANDED = ("landed", "newline")


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[Text(text=text)])


def _inject(monkeypatch, fault: str) -> None:
    real = jsonl._write_record

    def broken(path, record, *, fsync):
        if fault == "open":
            raise PermissionError(13, "cannot open for append", str(path))
        if fault == "partial":
            data = (json.dumps(record) + "\n").encode("utf-8")
            with open(path, "ab") as fh:
                fh.write(data[: len(data) // 2])
            raise OSError(28, "No space left on device")
        if fault == "newline":
            with open(path, "ab") as fh:
                fh.write(json.dumps(record).encode("utf-8"))
            raise OSError(28, "No space left on device")
        real(path, record, fsync=fsync)
        raise OSError(5, "fsync failed")

    monkeypatch.setattr(jsonl, "_write_record", broken)


def _heal(monkeypatch) -> None:
    monkeypatch.undo()


async def _seeded(tmp_path) -> tuple[JsonlStore, object]:
    store = JsonlStore(tmp_path)
    meta = await store.create_session(SID)
    await store.append(SID, SessionNode("u1", None, _msg("user", "hello")))
    await store.append(SID, SessionNode("a1", "u1", _msg("assistant", "hi")))
    return store, meta


def _reopened(tmp_path) -> JsonlStore:
    return JsonlStore(tmp_path, lock=False)


async def _assert_same_tree(live: JsonlStore, tmp_path) -> None:
    fresh = _reopened(tmp_path)
    live_meta, fresh_meta = await live.get_session(SID), await fresh.get_session(SID)
    assert (live_meta is None) == (fresh_meta is None)
    if fresh_meta is None:
        return
    assert live_meta.head_node_id == fresh_meta.head_node_id
    assert live_meta.cli_session_id == fresh_meta.cli_session_id
    assert [n.node_id for n in await live.load_path(SID)] == [
        n.node_id for n in await fresh.load_path(SID)
    ]
    assert sorted(live._nodes[SID]) == sorted(fresh._nodes[SID])


# ─── Finding 1: commit first, publish after ──────────────────────────────────


@pytest.mark.parametrize("fault", FAULTS)
async def test_failed_append_leaves_live_head_equal_to_reopened_head(
    tmp_path, monkeypatch, fault
) -> None:
    store, meta = await _seeded(tmp_path)
    _inject(monkeypatch, fault)
    with pytest.raises(OSError):
        await store.append(SID, SessionNode("u2", "a1", _msg("user", "next")))
    _heal(monkeypatch)

    expected = "u2" if fault in LANDED else "a1"
    assert (await _reopened(tmp_path).get_session(SID)).head_node_id == expected
    # The engine keeps the SessionMeta it was handed; that object must agree too.
    assert meta.head_node_id == expected
    assert ((await store.get_node(SID, "u2")) is not None) == (fault in LANDED)
    await _assert_same_tree(store, tmp_path)

    # The store keeps working, and a torn half-line is cut off at the next write.
    await store.append(SID, SessionNode("u3", expected, _msg("user", "again")))
    await _assert_same_tree(store, tmp_path)
    assert all(json.loads(line) for line in (tmp_path / f"{SID}.jsonl").read_bytes().splitlines())


@pytest.mark.parametrize("fault", FAULTS)
async def test_failed_set_head_leaves_live_head_equal_to_reopened_head(
    tmp_path, monkeypatch, fault
) -> None:
    store, meta = await _seeded(tmp_path)
    _inject(monkeypatch, fault)
    with pytest.raises(OSError):
        await store.set_head(SID, "u1")
    _heal(monkeypatch)

    expected = "u1" if fault in LANDED else "a1"
    assert (await _reopened(tmp_path).get_session(SID)).head_node_id == expected
    assert meta.head_node_id == expected
    await _assert_same_tree(store, tmp_path)


@pytest.mark.parametrize("fault", FAULTS)
async def test_failed_cli_session_write_leaves_live_handle_equal_to_reopened(
    tmp_path, monkeypatch, fault
) -> None:
    store, meta = await _seeded(tmp_path)
    _inject(monkeypatch, fault)
    with pytest.raises(OSError):
        await store.set_cli_session_id(SID, "cli-2")
    _heal(monkeypatch)

    expected = "cli-2" if fault in LANDED else None
    assert (await _reopened(tmp_path).get_session(SID)).cli_session_id == expected
    assert meta.cli_session_id == expected
    await _assert_same_tree(store, tmp_path)


@pytest.mark.parametrize("fault", FAULTS)
async def test_failed_event_write_leaves_live_events_equal_to_reopened(
    tmp_path, monkeypatch, fault
) -> None:
    store, _meta = await _seeded(tmp_path)
    await store.append_event(SID, TurnStarted(session_id=SID, seq=1, turn_id="t1", model="m", provider="p"))
    _inject(monkeypatch, fault)
    with pytest.raises(OSError):
        await store.append_event(SID, TurnStarted(session_id=SID, seq=2, turn_id="t2", model="m", provider="p"))
    _heal(monkeypatch)

    expected = [1, 2] if fault in LANDED else [1]
    assert [e.seq for e in await _reopened(tmp_path).events_since(SID, 0)] == expected
    assert [e.seq for e in await store.events_since(SID, 0)] == expected

    await store.append_event(SID, TurnStarted(session_id=SID, seq=3, turn_id="t3", model="m", provider="p"))
    assert [e.seq for e in await store.events_since(SID, 0)] == [
        e.seq for e in await _reopened(tmp_path).events_since(SID, 0)
    ] == [*expected, 3]


@pytest.mark.parametrize("fault", FAULTS)
async def test_failed_create_leaves_live_session_equal_to_reopened(
    tmp_path, monkeypatch, fault
) -> None:
    store = JsonlStore(tmp_path)
    _inject(monkeypatch, fault)
    with pytest.raises(OSError):
        await store.create_session(SID)
    _heal(monkeypatch)

    exists = fault in LANDED
    assert ((await _reopened(tmp_path).get_session(SID)) is not None) == exists
    assert ((await store.get_session(SID)) is not None) == exists
    await _assert_same_tree(store, tmp_path)


# ─── Finding 2: one duplicate-id rule, live and on replay ────────────────────


async def test_a_different_node_under_an_existing_id_is_refused(tmp_path) -> None:
    store, meta = await _seeded(tmp_path)
    size = (tmp_path / f"{SID}.jsonl").stat().st_size
    with pytest.raises(ValueError, match="u1"):
        await store.append(SID, SessionNode("u1", None, _msg("user", "rewritten")))
    assert (tmp_path / f"{SID}.jsonl").stat().st_size == size  # nothing written
    assert meta.head_node_id == "a1"
    assert (await store.get_node(SID, "u1")).message.text() == "hello"
    await _assert_same_tree(store, tmp_path)
    assert (await _reopened(tmp_path).get_node(SID, "u1")).message.text() == "hello"


async def test_the_same_node_again_only_moves_the_head(tmp_path) -> None:
    store, meta = await _seeded(tmp_path)
    log = tmp_path / f"{SID}.jsonl"
    size = log.stat().st_size

    # A retry of the append that is already the head writes nothing.
    await store.append(SID, SessionNode("a1", "u1", _msg("assistant", "hi"), ts=5.0))
    assert log.stat().st_size == size

    # The same node while the head is elsewhere moves the head, as append does.
    await store.set_head(SID, None)
    await store.append(SID, SessionNode("u1", None, _msg("user", "hello")))
    assert meta.head_node_id == "u1"
    await _assert_same_tree(store, tmp_path)
    assert not any(
        json.loads(line).get("t") == "node" and json.loads(line)["node"]["node_id"] == "u1"
        for line in log.read_bytes().splitlines()[2:]
    )


async def test_memory_store_follows_the_same_duplicate_rule() -> None:
    store = MemoryStore()
    meta = await store.create_session(SID)
    await store.append(SID, SessionNode("u1", None, _msg("user", "hello")))
    await store.append(SID, SessionNode("a1", "u1", _msg("assistant", "hi")))
    with pytest.raises(ValueError, match="u1"):
        await store.append(SID, SessionNode("u1", None, _msg("user", "rewritten")))
    assert (await store.get_node(SID, "u1")).message.text() == "hello"
    assert meta.head_node_id == "a1"
    await store.append(SID, SessionNode("u1", None, _msg("user", "hello")))
    assert meta.head_node_id == "u1"


# ─── Review round 1 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["log", "events"])
async def test_a_record_that_lost_only_its_newline_is_finished_before_the_next(
    tmp_path, monkeypatch, kind
) -> None:
    store, _meta = await _seeded(tmp_path)
    path = tmp_path / (f"{SID}.jsonl" if kind == "log" else f"{SID}.events.jsonl")

    async def write(n: int) -> None:
        if kind == "log":
            await store.append(SID, SessionNode(f"u{n}", None, _msg("user", str(n))))
        else:
            await store.append_event(SID, TurnStarted(
                session_id=SID, seq=n, turn_id=f"t{n}", model="m", provider="p"))

    await write(2)
    _inject(monkeypatch, "newline")
    with pytest.raises(OSError):
        await write(3)
    _heal(monkeypatch)
    assert not path.read_bytes().endswith(b"\n")

    await write(4)
    assert all(json.loads(line) for line in path.read_bytes().splitlines())
    fresh = _reopened(tmp_path)
    if kind == "log":
        assert (await fresh.get_session(SID)).head_node_id == "u4"
        assert {"u2", "u3", "u4"} <= set(fresh._nodes[SID])
        await _assert_same_tree(store, tmp_path)
    else:
        assert [e.seq for e in await fresh.events_since(SID, 0)] == [2, 3, 4]
        assert [e.seq for e in await store.events_since(SID, 0)] == [2, 3, 4]


async def test_a_tail_that_cannot_be_repaired_refuses_the_next_write(
    tmp_path, monkeypatch
) -> None:
    store, meta = await _seeded(tmp_path)
    _inject(monkeypatch, "newline")
    with pytest.raises(OSError):
        await store.append(SID, SessionNode("u2", "a1", _msg("user", "next")))
    _heal(monkeypatch)

    def cannot(*_args, **_kwargs):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(jsonl, "_repair_file", cannot, raising=False)
    with pytest.raises(OSError):
        await store.append(SID, SessionNode("u3", "u2", _msg("user", "again")))
    assert meta.head_node_id == "u2"
    assert await store.get_node(SID, "u3") is None
    await _assert_same_tree(store, tmp_path)

    _heal(monkeypatch)
    await store.append(SID, SessionNode("u3", "u2", _msg("user", "again")))
    assert (await _reopened(tmp_path).get_session(SID)).head_node_id == "u3"
    await _assert_same_tree(store, tmp_path)


async def test_a_retry_rereads_what_another_writer_appended_before_deciding(tmp_path) -> None:
    seed, _meta = await _seeded(tmp_path)
    seed.close()
    reader = JsonlStore(tmp_path)
    meta = await reader.get_session(SID)  # a snapshot taken without the lock
    assert meta.head_node_id == "a1"

    other = JsonlStore(tmp_path)
    await other.append(SID, SessionNode("u2", "a1", _msg("user", "elsewhere")))
    other.close()

    # The resend of a1 is a retry, but the head has moved since the snapshot,
    # so the retry's head move has to be written.
    await reader.append(SID, SessionNode("a1", "u1", _msg("assistant", "hi")))
    assert meta.head_node_id == "a1"
    assert (await _reopened(tmp_path).get_session(SID)).head_node_id == "a1"
    await _assert_same_tree(reader, tmp_path)


_HOLD_LOCK = """
import sys
from fluid.store.jsonl import JsonlStore
store = JsonlStore(sys.argv[1])
store.lock_session(sys.argv[2])
print("locked", flush=True)
sys.stdin.read()
"""


async def test_a_retry_is_refused_while_another_process_holds_the_session(tmp_path) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    from fluid.store.base import SessionLocked

    seed, _meta = await _seeded(tmp_path)
    seed.close()
    reader = JsonlStore(tmp_path)
    await reader.get_session(SID)
    repo = Path(__file__).resolve().parents[2]
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(tmp_path), SID],
        cwd=repo, env=dict(os.environ, PYTHONPATH=str(repo)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(SessionLocked):
            await reader.append(SID, SessionNode("a1", "u1", _msg("assistant", "hi")))
    finally:
        holder.communicate("", timeout=60)


async def test_a_failed_reread_marks_the_held_meta_unverified(tmp_path, monkeypatch) -> None:
    store, meta = await _seeded(tmp_path)
    _inject(monkeypatch, "landed")

    def unreadable(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(JsonlStore, "_parse_log", unreadable)
    with pytest.raises(OSError):
        await store.set_head(SID, "u1")
    # The record landed but could not be read back: the engine's copy still
    # says a1 and must not be trusted until a re-read succeeds.
    assert meta.head_node_id == "a1"
    assert meta.verified is False
    with pytest.raises(OSError):
        await store.get_session(SID)
    assert meta.verified is False

    _heal(monkeypatch)
    assert await store.get_session(SID) is meta
    assert meta.verified is True and meta.head_node_id == "u1"
