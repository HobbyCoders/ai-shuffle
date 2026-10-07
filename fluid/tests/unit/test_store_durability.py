"""JsonlStore survives what a killed process, a second process, or a damaged
log throws at it: torn lines, missing headers, cycles, dangling parents,
duplicate ids, concurrent writers, and a listing that must never read the
events file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from fluid.cli.config import list_sessions
from fluid.events.model import TurnStarted, event_to_dict
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store.base import CorruptSession, MemoryStore, SessionNode
from fluid.store.jsonl import JsonlStore

REPO = Path(__file__).resolve().parents[2]


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[Text(text=text)])


def _node(node_id: str, parent: str | None, text: str = "hi", *, ts: float = 1000.0) -> dict:
    return {"t": "node", "head": True, "node": {
        "node_id": node_id, "parent_id": parent, "ts": ts,
        "message": _msg("user" if node_id.startswith("u") else "assistant", text).to_dict(),
    }}


def _write_log(directory: Path, sid: str, records: list, *, tail: str = "") -> Path:
    path = directory / f"{sid}.jsonl"
    body = "".join(
        (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records
    )
    path.write_bytes((body + tail).encode("utf-8"))
    return path


def _meta(sid: str) -> dict:
    return {"t": "meta", "session_id": sid, "created_at": 1.0, "head_node_id": None,
            "cli_session_id": None, "project_root": None}


def _child(script: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )


# ─── torn and damaged lines ──────────────────────────────────────────────────


async def test_torn_last_line_loads_and_is_cut_off_at_next_write(tmp_path, caplog) -> None:
    sid = "ses_torn"
    path = _write_log(tmp_path, sid, [_meta(sid), _node("u1", None, "first")],
                      tail='{"t": "node", "node": {"node_id": "u2", "par')
    store = JsonlStore(tmp_path)
    with caplog.at_level(logging.WARNING):
        session = await store.get_session(sid)
    assert session is not None and session.head_node_id == "u1"
    assert [n.node_id for n in await store.load_path(sid)] == ["u1"]
    assert "skipped 1 unreadable line" in caplog.text and "line 3" in caplog.text

    await store.append(sid, SessionNode("a1", "u1", _msg("assistant", "reply")))
    lines = path.read_bytes().split(b"\n")
    assert all(json.loads(line) for line in lines if line)
    assert b"\r" not in path.read_bytes()
    assert [n.node_id for n in await JsonlStore(tmp_path, lock=False).load_path(sid)] == ["u1", "a1"]


async def test_torn_middle_line_is_skipped_with_one_warning(tmp_path, caplog) -> None:
    sid = "ses_mid"
    _write_log(tmp_path, sid, [_meta(sid), _node("u1", None), '{"t": "node", "nope',
                               _node("a1", "u1"), "not json either"])
    store = JsonlStore(tmp_path)
    with caplog.at_level(logging.WARNING):
        path = await store.load_path(sid)
    assert [n.node_id for n in path] == ["u1", "a1"]
    warnings = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(warnings) == 1
    assert "skipped 2 unreadable line(s), first at line 3" in warnings[0].getMessage()


async def test_missing_meta_still_loads_and_applies_head_and_cli_session(tmp_path) -> None:
    sid = "ses_nometa"
    _write_log(tmp_path, sid, [
        {"t": "head", "head_node_id": None},
        {"t": "cli_session", "cli_session_id": "cli-1"},
        _node("u1", None, ts=5.0), _node("a1", "u1", ts=6.0),
        {"t": "head", "head_node_id": "u1"},
    ])
    session = await JsonlStore(tmp_path).get_session(sid)
    assert session is not None
    assert session.session_id == sid
    assert session.head_node_id == "u1"
    assert session.cli_session_id == "cli-1"
    assert session.created_at == 5.0


async def test_log_with_nothing_recognisable_is_not_a_session(tmp_path) -> None:
    (tmp_path / "ses_junk.jsonl").write_text("garbage\n{\"x\": 1}\n")
    assert await JsonlStore(tmp_path).get_session("ses_junk") is None


async def test_failed_parse_does_not_poison_the_cache(tmp_path, monkeypatch) -> None:
    sid = "ses_once"
    _write_log(tmp_path, sid, [_meta(sid), _node("u1", None)])
    store = JsonlStore(tmp_path)
    real = store._parse_log
    calls = {"n": 0}

    def flaky(session_id, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(session_id, path)

    monkeypatch.setattr(store, "_parse_log", flaky)
    with pytest.raises(OSError):
        await store.get_session(sid)
    assert (await store.get_session(sid)) is not None
    await store.append(sid, SessionNode("a1", "u1", _msg("assistant", "ok")))


async def test_duplicate_node_id_keeps_the_first_record(tmp_path, caplog) -> None:
    sid = "ses_dup"
    _write_log(tmp_path, sid, [_meta(sid), _node("u1", None, "original"),
                               _node("u1", None, "rewritten")])
    with caplog.at_level(logging.WARNING):
        path = await JsonlStore(tmp_path).load_path(sid)
    assert [n.message.text() for n in path] == ["original"]
    assert "duplicate node record" in caplog.text


# ─── tree walks ──────────────────────────────────────────────────────────────


async def test_cycle_raises_corrupt_session_naming_the_node(tmp_path) -> None:
    sid = "ses_cycle"
    _write_log(tmp_path, sid, [_meta(sid), _node("u1", "a1"), _node("a1", "u1")])
    store = JsonlStore(tmp_path)
    with pytest.raises(CorruptSession) as caught:
        await asyncio.wait_for(store.load_path(sid), timeout=5)
    assert caught.value.session_id == sid and caught.value.node_id in {"u1", "a1"}
    # The listing skips it instead of failing.
    _write_log(tmp_path, "ses_fine", [_meta("ses_fine"), _node("u1", None, "fine")])
    rows = await store.recent_sessions(limit=10)
    assert [r.session_id for r in rows] == ["ses_fine"]


async def test_memory_store_cycle_raises_too() -> None:
    store = MemoryStore()
    meta = await store.create_session("m")
    store._nodes["m"] = {
        "a": SessionNode("a", "b", _msg("user", "x")),
        "b": SessionNode("b", "a", _msg("assistant", "y")),
    }
    meta.head_node_id = "a"
    with pytest.raises(CorruptSession):
        await store.load_path("m")


async def test_dangling_parent_returns_partial_path_and_lists(tmp_path, caplog) -> None:
    sid = "ses_dangle"
    _write_log(tmp_path, sid, [_meta(sid), _node("a1", "u_missing", "reply"),
                               _node("u2", "a1", "next")])
    store = JsonlStore(tmp_path)
    with caplog.at_level(logging.WARNING):
        path = await store.load_path(sid)
    assert [n.node_id for n in path] == ["a1", "u2"]
    assert "u_missing" in caplog.text
    rows = await store.recent_sessions(limit=5)
    assert rows[0].session_id == sid and rows[0].message_count == 2


async def test_set_head_refuses_an_unknown_node(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode("u1", None, _msg("user", "hi")))
    with pytest.raises(ValueError):
        await store.set_head(sid, "nowhere")
    await store.set_head(sid, None)
    assert (await store.get_session(sid)).head_node_id is None
    assert (await JsonlStore(tmp_path).get_session(sid)).head_node_id is None


# ─── head moves with the node ────────────────────────────────────────────────


async def test_append_writes_node_and_head_as_one_record(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode("u1", None, _msg("user", "q")))
    await store.append(sid, SessionNode("a1", "u1", _msg("assistant", "reply")))
    path = tmp_path / f"{sid}.jsonl"
    records = [json.loads(line) for line in path.read_bytes().split(b"\n") if line]
    assert [r["t"] for r in records] == ["meta", "node", "node"]
    assert records[-1]["head"] is True
    assert b"\r" not in path.read_bytes()

    # Drop the last record entirely: the reload loses that node AND its head
    # move together, so the head never points past what is on disk.
    path.write_bytes(b"\n".join(json.dumps(r).encode() for r in records[:-1]) + b"\n")
    reloaded = await JsonlStore(tmp_path).load_path(sid)
    assert [n.node_id for n in reloaded] == ["u1"]


async def test_legacy_separate_head_record_still_replays(tmp_path) -> None:
    sid = "ses_legacy"
    node = _node("u1", None)
    del node["head"]
    _write_log(tmp_path, sid, [_meta(sid), node, {"t": "head", "head_node_id": "u1"}])
    assert [n.node_id for n in await JsonlStore(tmp_path).load_path(sid)] == ["u1"]


async def test_fsync_flag_syncs_every_record(tmp_path, monkeypatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    store = JsonlStore(tmp_path, fsync=True)
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode("u1", None, _msg("user", "q")))
    assert len(synced) == 2
    monkeypatch.setenv("FLUID_STORE_FSYNC", "1")
    assert JsonlStore(tmp_path)._fsync is True
    monkeypatch.delenv("FLUID_STORE_FSYNC")
    assert JsonlStore(tmp_path)._fsync is False


# ─── concurrent writers and the session lock ─────────────────────────────────

_APPENDER = """
import asyncio, sys
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore

async def main(directory, sid, tag, count):
    store = JsonlStore(directory, lock=False)
    for i in range(count):
        text = f"{tag}-{i} " + "x" * (100 + (i % 9) * 50)
        await store.append(sid, SessionNode(f"{tag}{i}", None, Message(role="user", content=[Text(text=text)])))

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])))
"""


def test_two_processes_appending_never_tear_a_line(tmp_path) -> None:
    sid = "ses_race"
    asyncio.run(JsonlStore(tmp_path, lock=False).create_session(sid))
    env = dict(os.environ, PYTHONPATH=str(REPO))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(_APPENDER), str(tmp_path), sid, tag, "300"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for tag in ("p", "q")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    raw = (tmp_path / f"{sid}.jsonl").read_bytes()
    lines = [line for line in raw.split(b"\n") if line]
    records = [json.loads(line) for line in lines]  # every line parses
    assert len(records) == 601
    assert b"\r" not in raw
    store = JsonlStore(tmp_path, lock=False)
    asyncio.run(store.get_session(sid))
    assert len(store._nodes[sid]) == 600


_LOCK_PROBE = """
import asyncio, sys
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store.base import SessionLocked, SessionNode
from fluid.store.jsonl import JsonlStore

async def main(directory, sid):
    store = JsonlStore(directory)
    assert await store.get_session(sid) is not None  # reading is always allowed
    try:
        await store.append(sid, SessionNode("child", None, Message(role="user", content=[Text(text="x")])))
    except SessionLocked as exc:
        print(exc)
        return 3
    return 0

sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
"""


def test_second_process_on_a_locked_session_is_refused(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    sid = asyncio.run(store.create_session("ses_locked")).session_id
    result = _child(_LOCK_PROBE, str(tmp_path), sid)
    assert result.returncode == 3, result.stderr
    assert f"session {sid} is open in another process" in result.stdout
    # Released on close: the same probe now writes.
    store.close()
    result = _child(_LOCK_PROBE, str(tmp_path), sid)
    assert result.returncode == 0, result.stderr


def test_lock_session_fails_fast_and_cli_reports_exit_2(tmp_path) -> None:

    store = JsonlStore(tmp_path)
    sid = asyncio.run(store.create_session("ses_cli")).session_id
    probe = """
    import asyncio, sys
    from fluid.store.base import SessionLocked
    from fluid.store.jsonl import JsonlStore
    try:
        JsonlStore(sys.argv[1]).lock_session(sys.argv[2])
    except SessionLocked:
        sys.exit(3)
    sys.exit(0)
    """
    assert _child(probe, str(tmp_path), sid).returncode == 3
    # The CLI turns the same refusal into exit code 2 with a plain message.
    cli = """
    import sys
    from fluid.cli.__main__ import main
    sys.exit(main(["run", "--store", sys.argv[1], "--resume", sys.argv[2], "hello"]))
    """
    result = _child(cli, str(tmp_path), sid)
    assert result.returncode == 2, result.stderr
    assert f"session {sid} is open in another process" in result.stderr


async def test_two_stores_in_one_process_share_the_lock(tmp_path) -> None:
    first = JsonlStore(tmp_path)
    sid = (await first.create_session()).session_id
    second = JsonlStore(tmp_path)
    await second.append(sid, SessionNode("u1", None, _msg("user", "q")))
    first.close()
    await second.append(sid, SessionNode("a1", "u1", _msg("assistant", "r")))
    assert [n.node_id for n in await first.load_path(sid)] == []  # first never reloaded
    assert [n.node_id for n in await JsonlStore(tmp_path).load_path(sid)] == ["u1", "a1"]


# ─── events stay lazy and tolerant ───────────────────────────────────────────


async def test_recent_sessions_never_reads_the_events_file(tmp_path, monkeypatch) -> None:
    sid = "ses_ev"
    _write_log(tmp_path, sid, [_meta(sid), _node("u1", None, "hello")])
    (tmp_path / f"{sid}.events.jsonl").write_text("{not json at all\n")
    store = JsonlStore(tmp_path)
    parsed: list[str] = []
    real = store._ensure_events_loaded
    monkeypatch.setattr(
        store, "_ensure_events_loaded", lambda s: (parsed.append(s), real(s))
    )
    rows = await store.recent_sessions(limit=5)
    assert [r.session_id for r in rows] == [sid]
    await store.session_slice(sid)
    await store.search(["hello"])
    assert parsed == []
    assert sid not in store._events_loaded
    # Reading events later tolerates the bad line instead of raising.
    assert await store.events_since(sid, 0) == []
    assert parsed == [sid]


async def test_events_file_skips_unknown_class_and_torn_tail(tmp_path, caplog) -> None:
    sid = "ses_evbad"
    _write_log(tmp_path, sid, [_meta(sid)])
    good = event_to_dict(TurnStarted(session_id=sid, seq=1, model="m", provider="p"))
    epath = tmp_path / f"{sid}.events.jsonl"
    epath.write_bytes(
        (json.dumps(good) + "\n").encode()
        + b'{"_cls": "NoSuchEvent", "session_id": "x", "seq": 2}\n'
        + b'{"_cls": "TurnStarted", "sess'
    )
    store = JsonlStore(tmp_path)
    with caplog.at_level(logging.WARNING):
        events = await store.events_since(sid, 0)
    # An unknown class is either decoded generically or skipped, never fatal.
    assert events[0].seq == 1 and {e.seq for e in events} <= {1, 2}
    assert "unreadable line(s)" in caplog.text and "line 3" in caplog.text
    # The torn tail goes at the next write to the session.
    await store.append_event(sid, TurnStarted(session_id=sid, seq=2, model="m", provider="p"))
    on_disk = [json.loads(line)["seq"] for line in epath.read_bytes().splitlines() if line]
    assert on_disk == [1, 2, 2]


# ─── the CLI listing ─────────────────────────────────────────────────────────


def test_list_sessions_tolerates_torn_lines_and_ranks_head_only_moves(tmp_path) -> None:
    old, newer = "ses_old", "ses_newer"
    _write_log(tmp_path, newer, [_meta(newer), _node("u1", None, "newer", ts=2000.0)])
    _write_log(tmp_path, old, [_meta(old), _node("u1", None, "old", ts=1000.0),
                               _node("a1", "u1", "r", ts=1001.0)],
               tail='{"t": "node", "node": {"node_id": "u2", "par')
    long_ago = time.time() - 86400
    os.utime(tmp_path / f"{old}.jsonl", (long_ago, long_ago))
    rows = list_sessions(tmp_path)
    assert [r["session_id"] for r in rows] == [newer, old]
    assert next(r for r in rows if r["session_id"] == old)["nodes"] == 2

    # A rewind on the old session writes a head record only; it ranks first now.
    store = JsonlStore(tmp_path)
    asyncio.run(store.set_head(old, "u1"))
    store.close()
    assert [r["session_id"] for r in list_sessions(tmp_path)] == [old, newer]
    assert [r["session_id"] for r in list_sessions(tmp_path, limit=1)] == [old]
    assert list_sessions(tmp_path, limit=0) == []


def test_list_sessions_parses_only_the_newest_log_for_limit_one(tmp_path, monkeypatch) -> None:
    import fluid.cli.config as config

    stamp = time.time() - 3600
    for i in range(5):
        sid = f"ses_{i}"
        _write_log(tmp_path, sid, [_meta(sid), _node("u1", None, sid, ts=float(i))])
        os.utime(tmp_path / f"{sid}.jsonl", (stamp + i, stamp + i))
    parsed: list[str] = []
    real = config._session_row
    monkeypatch.setattr(
        config, "_session_row", lambda log, mtime: (parsed.append(log.name), real(log, mtime))[1]
    )
    assert [r["session_id"] for r in list_sessions(tmp_path, limit=1)] == ["ses_4"]
    assert parsed == ["ses_4.jsonl"]


# ─── ledger ──────────────────────────────────────────────────────────────────


async def test_ledger_write_leaves_no_temp_file(tmp_path) -> None:
    from fluid.tools.builtins.ledger import handler
    from fluid.tools.registry import ToolCtx

    ctx = ToolCtx(cwd=str(tmp_path), session_id="ses_l")
    out = await handler({"action": "add", "section": "state", "text": "step one"}, ctx)
    assert not out.is_error
    from fluid.state import project_state_root
    ledger_dir = project_state_root(ctx) / "ledger"
    assert [p.name for p in ledger_dir.iterdir()] == ["ses_l.md"]
    assert "step one" in (ledger_dir / "ses_l.md").read_text(encoding="utf-8")
