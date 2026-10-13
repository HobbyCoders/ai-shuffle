"""Audit fix: the read-before-edit ledger survives an engine rebuild.

``ctx.read_files`` is process memory that only Read and Write add to, but the
session outlives the process: a resumed session used to treat a file the model
demonstrably Read as unread and refuse the next Edit, pointing at a Read that
was sitting right there in the history. The engine now replays the stored
transcript into the ledger on its first turn.
"""

from __future__ import annotations

import json

from fluid.engine.native import NativeLoop
from fluid.events import ToolResult, UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore, SessionNode, new_id
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _text,
    _tool_call,
)


def _loop(store, sid: str, tmp_path, scripts) -> NativeLoop:
    """One engine over an existing session — a fresh registry and a fresh ctx,
    exactly what ``build_engine`` hands a resumed session."""
    registry = ToolRegistry()
    register_builtins(registry)
    return NativeLoop(
        provider=ScriptedProvider(scripts), store=store, model="m",
        session_id=sid, registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(cwd=str(tmp_path), session_id=sid),
    )


async def _turn(loop: NativeLoop, content: str) -> list:
    await loop.submit(UserTurn(content=content))
    await loop.wait_turn()
    return loop.bus.log(loop.session_id)


# ─── Acceptance: Read → rebuild → Edit ───────────────────────────────────────


async def test_edit_after_an_engine_rebuild_needs_no_second_read(tmp_path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")
    store = MemoryStore()
    sid = (await store.create_session()).session_id

    first = _loop(store, sid, tmp_path, [
        _tool_call("t1", "Read", json.dumps({"file_path": str(target)})),
        _text("read it"),
    ])
    await _turn(first, "read the file")
    assert str(target.resolve()) in first.ctx.read_files

    # The rebuild: same store and session, a brand-new engine and ToolCtx —
    # and, in the audited session, 180 nodes between the Read and the Edit.
    second = _loop(store, sid, tmp_path, [
        _tool_call("t2", "Edit", json.dumps({
            "file_path": str(target),
            "old_string": "value = 1",
            "new_string": "value = 2",
        })),
        _text("done"),
    ])
    assert second.ctx.read_files == set()  # nothing carried over in memory
    events = await _turn(second, "bump the value")

    result = next(
        e for e in events if isinstance(e, ToolResult) and e.tool_use_id == "t2"
    )
    assert not result.is_error, result.output
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert str(target.resolve()) in second.ctx.read_files


async def test_a_file_never_read_is_still_refused_after_a_rebuild(tmp_path) -> None:
    target = tmp_path / "other.py"
    target.write_text("value = 1\n", encoding="utf-8")
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await _turn(_loop(store, sid, tmp_path, [_text("hello")]), "say hi")

    events = await _turn(
        _loop(store, sid, tmp_path, [
            _tool_call("t9", "Edit", json.dumps({
                "file_path": str(target),
                "old_string": "value = 1",
                "new_string": "value = 2",
            })),
            _text("done"),
        ]),
        "bump it",
    )
    result = next(
        e for e in events if isinstance(e, ToolResult) and e.tool_use_id == "t9"
    )
    assert result.is_error and "must Read" in result.output
    assert target.read_text(encoding="utf-8") == "value = 1\n"


# ─── The replay itself ───────────────────────────────────────────────────────


async def _seed(store, sid: str, use_input: dict, name: str = "Read", *, error=False):
    """Append one assistant tool call + its user-side result to a session."""
    tuid = new_id("call")
    head = (await store.get_session(sid)).head_node_id
    call = SessionNode(
        node_id=new_id("node"), parent_id=head,
        message=Message(role="assistant", content=[
            ToolUseBlock(id=tuid, name=name, input=use_input),
        ]),
    )
    await store.append(sid, call)
    result = SessionNode(
        node_id=new_id("node"), parent_id=call.node_id,
        message=Message(role="user", content=[
            ToolResultBlock(
                tool_use_id=tuid, content=[Text("ok")], is_error=error,
            ),
        ]),
    )
    await store.append(sid, result)


async def _rehydrated(store, sid: str, tmp_path) -> set[str]:
    ctx = ToolCtx(cwd=str(tmp_path), session_id=sid)
    loop = NativeLoop(
        provider=ScriptedProvider([_text("ok")]), store=store, model="m",
        session_id=sid, ctx=ctx,
    )
    await loop._rehydrate_read_files(sid, (await store.get_session(sid)).head_node_id)
    return ctx.read_files


async def test_replay_records_reads_and_writes_as_resolved_paths(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await _seed(store, sid, {"file_path": "rel/a.py"})  # cwd-relative
    await _seed(store, sid, {"file_path": str(tmp_path / "b.py")}, name="Write")
    seeded = await _rehydrated(store, sid, tmp_path)
    assert seeded == {
        str((tmp_path / "rel" / "a.py").resolve()),
        str((tmp_path / "b.py").resolve()),
    }


async def test_replay_skips_failed_reads(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await _seed(store, sid, {"file_path": str(tmp_path / "gone.py")}, error=True)
    assert await _rehydrated(store, sid, tmp_path) == set()


async def test_replay_tolerates_old_and_malformed_records(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await _seed(store, sid, {})  # a Read logged without its path
    await _seed(store, sid, {"file_path": ""})
    await _seed(store, sid, {"file_path": 17})
    await _seed(store, sid, {"pattern": "x"}, name="Grep")  # not a ledger tool
    assert await _rehydrated(store, sid, tmp_path) == set()


async def test_replay_runs_once_per_engine(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await _seed(store, sid, {"file_path": str(tmp_path / "a.py")})
    ctx = ToolCtx(cwd=str(tmp_path), session_id=sid)
    loop = NativeLoop(
        provider=ScriptedProvider([_text("ok")]), store=store, model="m",
        session_id=sid, ctx=ctx,
    )
    head = (await store.get_session(sid)).head_node_id
    await loop._rehydrate_read_files(sid, head)
    ctx.read_files.clear()  # a later Edit invalidating nothing must not re-seed
    await loop._rehydrate_read_files(sid, head)
    assert ctx.read_files == set()
