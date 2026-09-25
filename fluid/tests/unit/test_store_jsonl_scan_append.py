"""JsonlStore: the recall scan over raw logs, the append path, the tail
repair and the per-session write lock, driven directly."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from fluid.events.model import TurnStarted
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store import jsonl as jsonl_mod
from fluid.store.base import SessionLocked, SessionNode
from fluid.store.jsonl import JsonlStore

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="flock and POSIX modes")


def _msg(role: str, text: str) -> dict:
    return Message(role=role, content=[Text(text=text)]).to_dict()


def _node(node_id: str, parent: str | None, role: str, text: str, ts: float) -> dict:
    return {"t": "node", "head": True, "node": {
        "node_id": node_id, "parent_id": parent, "ts": ts, "message": _msg(role, text),
    }}


def _meta(sid: str, project_root: str | None = None) -> dict:
    return {"t": "meta", "session_id": sid, "created_at": 1.0, "head_node_id": None,
            "cli_session_id": None, "project_root": project_root}


def _write(path: Path, *records, tail: str = "") -> Path:
    body = "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records)
    path.write_text(body + tail, encoding="utf-8")
    return path


# ─── _scan_log / search ──────────────────────────────────────────────────────


async def test_search_scores_matches_reads_the_project_and_titles_the_hit(tmp_path) -> None:
    _write(tmp_path / "s1.jsonl", _meta("s1", "/repo/a"),
           _node("u1", None, "user", "Where is the Needle?", 10.0),
           _node("a1", "u1", "assistant", "the needle and the NEEDLE", 20.0))
    _write(tmp_path / "s2.jsonl", _meta("s2"), _node("u1", None, "user", "haystack", 5.0))

    hits = await JsonlStore(tmp_path, lock=False).search(["needle"])

    assert [h.session_id for h in hits] == ["s1"]
    hit = hits[0]
    assert hit.score == 2  # one per matching text block
    assert hit.project_root == "/repo/a"
    assert hit.last_ts == 20.0
    assert hit.title == "Where is the Needle?"
    assert [role for role, _ in hit.texts] == ["user", "assistant"]


async def test_headerless_log_is_scanned_whole_with_no_project(tmp_path) -> None:
    _write(tmp_path / "raw.jsonl", _node("u1", None, "user", "needle on line one", 3.0))

    hits = await JsonlStore(tmp_path, lock=False).search(["needle"])

    assert [h.session_id for h in hits] == ["raw"]
    assert hits[0].project_root is None and hits[0].score == 1


async def test_torn_header_and_garbage_lines_do_not_stop_the_scan(tmp_path) -> None:
    _write(tmp_path / "torn.jsonl", '{"t": "meta", "needle',
           "needle but not json",
           json.dumps({"t": "head", "head_node_id": "needle"}),
           ["needle", "a list"],
           _node("u1", None, "user", "real needle", 4.0),
           tail='{"t": "node", "node": {"needle')

    hits = await JsonlStore(tmp_path, lock=False).search(["needle"])

    assert len(hits) == 1 and hits[0].score == 1
    assert hits[0].texts == [("user", "real needle")]


@pytest.mark.parametrize("bad", [
    {"t": "node", "node": {"node_id": "x", "ts": "needle-time", "message": _msg("user", "needle")}},
    {"t": "node", "node": ["needle"]},
    {"t": "node", "node": {"node_id": "x", "ts": 1, "message": "needle"}},
    {"t": "node", "node": {"node_id": "x", "ts": 1, "message": {"role": "user", "content": 7, "k": "needle"}}},
], ids=["string-ts", "list-node", "string-message", "int-content"])
async def test_one_malformed_record_does_not_break_search_for_every_session(tmp_path, bad) -> None:
    """Regression: a parsable record of the wrong shape raised out of
    ``_scan_log`` (ValueError/AttributeError/TypeError) and failed the whole
    SessionSearch sweep, not just the one line."""
    _write(tmp_path / "good.jsonl", _meta("good"), _node("u1", None, "user", "needle here", 9.0))
    _write(tmp_path / "bad.jsonl", _meta("bad"), _node("u1", None, "user", "fine", 1.0), bad)

    hits = await JsonlStore(tmp_path, lock=False).search(["needle"])

    assert "good" in {h.session_id for h in hits}


async def test_unreadable_log_scores_nothing(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path / "locked.jsonl", _meta("locked"), _node("u1", None, "user", "needle", 1.0))
    store = JsonlStore(tmp_path, lock=False)
    real_open = Path.open

    def deny(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    hit = store._scan_log(path, "locked", ["needle"])

    assert hit.score == 0 and hit.texts == []


async def test_search_excludes_the_named_session_and_the_events_files(tmp_path) -> None:
    _write(tmp_path / "me.jsonl", _meta("me"), _node("u1", None, "user", "needle", 1.0))
    _write(tmp_path / "me.events.jsonl", {"_cls": "TurnStarted", "note": "needle"})
    _write(tmp_path / "other.jsonl", _meta("other"), _node("u1", None, "user", "needle", 1.0))

    hits = await JsonlStore(tmp_path, lock=False).search(["needle"], exclude_session_id="other")

    assert [h.session_id for h in hits] == ["me"]


async def test_matches_per_session_are_capped(tmp_path) -> None:
    nodes = [_node(f"u{i}", None if i == 0 else f"u{i - 1}", "user", f"needle {i}", float(i))
             for i in range(6)]
    _write(tmp_path / "many.jsonl", _meta("many"), *nodes)

    hit = (await JsonlStore(tmp_path, lock=False).search(["needle"]))[0]

    assert hit.score == 6
    assert len(hit.texts) == jsonl_mod._MAX_MATCHES_PER_SESSION


# ─── append path: open mode, fsync switch, one write per record ──────────────


@POSIX_ONLY
def test_open_append_creates_the_log_0644_and_appends(tmp_path) -> None:
    path = tmp_path / "x.jsonl"
    old = os.umask(0)
    try:
        fd = jsonl_mod._open_append(path)
    finally:
        os.umask(old)
    try:
        os.write(fd, b"one\n")
    finally:
        os.close(fd)
    fd = jsonl_mod._open_append(path)
    try:
        os.lseek(fd, 0, os.SEEK_SET)  # O_APPEND ignores the position
        os.write(fd, b"two\n")
    finally:
        os.close(fd)
    assert path.read_bytes() == b"one\ntwo\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize("env, flag, expected", [
    ({}, None, False),
    ({"FLUID_STORE_FSYNC": "1"}, None, True),
    ({"FLUID_STORE_FSYNC": "yes"}, None, False),  # only "1" switches it on
    ({"FLUID_STORE_FSYNC": "1"}, False, False),   # the argument wins
    ({}, True, True),
])
async def test_fsync_switch(tmp_path, monkeypatch, env, flag, expected) -> None:
    monkeypatch.delenv("FLUID_STORE_FSYNC", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    synced: list[int] = []
    monkeypatch.setattr(jsonl_mod.os, "fsync", lambda fd: synced.append(fd))

    store = JsonlStore(tmp_path, fsync=flag)
    await store.create_session("s")
    await store.append("s", SessionNode("u1", None, Message(role="user", content=[Text(text="hi")])))
    await store.append_event("s", TurnStarted(session_id="s", seq=1, model="m", provider="p"))
    store.close()

    assert (len(synced) == 3) is expected and (not synced) is (not expected)


async def test_every_record_is_one_write_call(tmp_path, monkeypatch) -> None:
    writes: list[bytes] = []
    real_write = os.write

    def spy(fd, data):
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(jsonl_mod.os, "write", spy)
    store = JsonlStore(tmp_path)
    await store.create_session("s")
    await store.append("s", SessionNode("u1", None, Message(role="user", content=[Text(text="x" * 50_000)])))
    store.close()

    assert len(writes) == 2
    assert all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in writes)


# ─── tail repair ─────────────────────────────────────────────────────────────


async def test_tail_repair_leaves_a_line_another_writer_completed(tmp_path, caplog) -> None:
    path = _write(tmp_path / "s.jsonl", _meta("s"), _node("u1", None, "user", "hi", 1.0),
                  tail='{"t": "node", "no')
    store = JsonlStore(tmp_path, lock=False)
    assert await store.get_session("s") is not None
    # Someone else appended after our parse: the fragment is no longer the tail.
    with path.open("ab") as fh:
        fh.write(b'de": 1}\n')
    before = path.read_bytes()

    with caplog.at_level(logging.WARNING):
        await store.set_cli_session_id("s", "cli-1")

    assert path.read_bytes().startswith(before)
    assert "cut off a torn last line" not in caplog.text


async def test_tail_repair_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog) -> None:
    path = _write(tmp_path / "s.jsonl", _meta("s"), tail='{"t": "no')
    store = JsonlStore(tmp_path, lock=False)
    assert await store.get_session("s") is not None
    real_open = Path.open

    def deny(self, mode="r", *args, **kwargs):
        if self == path and mode == "r+b":
            raise PermissionError(13, "read-only")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    with caplog.at_level(logging.WARNING):
        await store.set_cli_session_id("s", "cli-1")

    assert "could not cut off the torn last line" in caplog.text
    assert path.read_text(encoding="utf-8").endswith('"cli_session_id": "cli-1"}\n')


async def test_headerless_log_without_timestamps_dates_from_the_file(tmp_path) -> None:
    path = _write(tmp_path / "s.jsonl", {"t": "cli_session", "cli_session_id": "c"})
    os.utime(path, (1_000_000, 1_000_000))

    meta = await JsonlStore(tmp_path, lock=False).get_session("s")

    assert meta.created_at == 1_000_000 and meta.cli_session_id == "c"


# ─── the write lock ──────────────────────────────────────────────────────────


def _foreign_flock(path: Path):
    """Take the lock file the way another process would: a separate open file
    description, which ``flock`` treats as a different owner."""
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@POSIX_ONLY
async def test_lock_is_held_until_close_then_free(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    await store.create_session("s")
    lock_path = tmp_path / "s.lock"

    assert _foreign_flock(lock_path) is None  # held by the store

    store.close()
    fd = _foreign_flock(lock_path)
    assert fd is not None
    os.close(fd)
    await store.aclose()  # idempotent


@POSIX_ONLY
async def test_a_foreign_holder_refuses_writes_without_leaking_state(tmp_path) -> None:
    fd = _foreign_flock(tmp_path / "s.lock")
    assert fd is not None
    try:
        store = JsonlStore(tmp_path)
        with pytest.raises(SessionLocked):
            await store.create_session("s")
        assert jsonl_mod._lock_key(tmp_path / "s.lock") not in jsonl_mod._HELD
        assert not (tmp_path / "s.jsonl").exists()
    finally:
        os.close(fd)
    await store.create_session("s")  # free now: the refusal cached nothing
    store.close()


@POSIX_ONLY
async def test_shared_in_process_lock_is_released_by_the_last_store(tmp_path) -> None:
    first, second = JsonlStore(tmp_path), JsonlStore(tmp_path)
    await first.create_session("s")
    second.lock_session("s")
    key = jsonl_mod._lock_key(tmp_path / "s.lock")
    assert jsonl_mod._HELD[key].refs == 2

    first.close()
    assert _foreign_flock(tmp_path / "s.lock") is None  # the second still holds it
    second.close()
    assert key not in jsonl_mod._HELD
    fd = _foreign_flock(tmp_path / "s.lock")
    assert fd is not None
    os.close(fd)


def test_lock_false_never_takes_a_lock(tmp_path) -> None:
    store = JsonlStore(tmp_path, lock=False)
    store.lock_session("s")
    assert not (tmp_path / "s.lock").exists()


def test_release_all_drops_every_held_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jsonl_mod, "_HELD", {})
    jsonl_mod._acquire_lock(tmp_path / "a.lock", "a")
    jsonl_mod._acquire_lock(tmp_path / "b.lock", "b")
    jsonl_mod._release_lock(tmp_path / "missing.lock")  # never held: no-op

    jsonl_mod._release_all()

    assert jsonl_mod._HELD == {}
    if sys.platform != "win32":
        for name in ("a.lock", "b.lock"):
            fd = _foreign_flock(tmp_path / name)
            assert fd is not None
            os.close(fd)
