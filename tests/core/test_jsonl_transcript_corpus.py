"""Transcript-parsing tests driven by a corpus of realistic Claude CLI JSONL.

The fixtures below reproduce the line shapes the CLI actually writes (checked
against real 2.1.x transcripts): per-content-block assistant lines that repeat
``message.id`` and ``usage``, ``toolUseResult`` sidecars, uuid-less bookkeeping
lines (``queue-operation``, ``last-prompt``, ``custom-title``,
``file-history-*``), ``attachment`` lines, ``stop_hook_summary`` system lines,
sidechains and sub-agent transcripts in both layouts, compaction boundaries and
``summary`` lines, the legacy SDK shapes (<=0.1.30 ``Task`` with every block in
one entry, >=0.1.48 ``Agent``, 0.1.65+ ``server_tool_use``), and on-disk damage
(torn last line, invalid line mid-file, non-UTF-8 bytes, CRLF, NUL tail, empty
file, non-object lines).

Areas covered: message extraction and ordering, uuid/parent handling, the
hidden-subtype filter, thinking blocks, tool pairing, the parse caches and
windowed store reads, rewind (``jsonl_rewind`` + ``rewind_db_sync``), history
backfill into the event store, and the read/write error paths.

Everything is hermetic: the Claude projects root is a tmp dir, the DB is the
autouse SQLite shim, and store loaders are in-memory callables.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.core.history_backfill as hb
import app.core.rewind_db_sync as rds
from app.core import jsonl_parser as jp
from app.core.jsonl_rewind import (
    checkpoints_from_entries,
    compute_rewind_keep_count,
    jsonl_rewind_service,
)
from app.db import database
from app.db import session_events as se
from app.db import sessions as sessions_db
from tests.shared_fixtures import reset_backfill_memo  # noqa: F401  (autouse fixture)

# UUID-shaped so the fluid-session probe (prefix-gated) never claims it.
SID = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"
WD = "/workspace/demo"
MODEL = "claude-sonnet-4-5-20250929"


# =============================================================================
# Fixture library: line builders
# =============================================================================

def _ts(i: int) -> str:
    return f"2026-03-01T12:{i // 60:02d}:{i % 60:02d}.000Z"


def _envelope(type_: str, uuid: Optional[str], parent: Optional[str], i: int, **extra) -> Dict[str, Any]:
    """The per-line envelope every CLI 2.x conversation line carries."""
    e: Dict[str, Any] = {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": WD,
        "sessionId": SID,
        "version": "2.1.40",
        "gitBranch": "main",
        "type": type_,
        "timestamp": _ts(i),
    }
    if uuid is not None:
        e["uuid"] = uuid
    e.update(extra)
    return e


def user(uuid, text, *, parent=None, i=0, **extra):
    return _envelope("user", uuid, parent, i,
                     message={"role": "user", "content": text}, **extra)


def user_blocks(uuid, blocks, *, parent=None, i=0, tool_use_result=None, **extra):
    e = _envelope("user", uuid, parent, i,
                  message={"role": "user", "content": blocks}, **extra)
    if tool_use_result is not None:
        e["toolUseResult"] = tool_use_result
    return e


def usage(inp, out, *, cache_read=0, cache_create=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "service_tier": "standard",
    }


def assistant(uuid, blocks, *, parent=None, i=0, msg_id=None, use=None,
              model=MODEL, **extra):
    message: Dict[str, Any] = {
        "model": model,
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "stop_reason": None,
        "usage": use if use is not None else usage(1, 1),
    }
    if msg_id is not None:
        message["id"] = msg_id
    return _envelope("assistant", uuid, parent, i, message=message,
                     requestId=f"req_{msg_id or uuid}", **extra)


def split_assistant(uuids, blocks, *, parent, i, msg_id, use):
    """CLI 2.x: one line per content block, all sharing message.id + usage."""
    out = []
    prev = parent
    for n, (u, b) in enumerate(zip(uuids, blocks)):
        out.append(assistant(u, [b], parent=prev, i=i + n, msg_id=msg_id, use=use))
        prev = u
    return out


def text(t):
    return {"type": "text", "text": t}


def thinking(t):
    return {"type": "thinking", "thinking": t, "signature": "EqQBCkYIBxgCKkA"}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, content, *, is_error=False):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        b["is_error"] = True
    return b


def system(uuid, subtype, *, content="", parent=None, i=0, **extra):
    return _envelope("system", uuid, parent, i, subtype=subtype,
                     content=content, level="info", **extra)


def queue_op(operation, content="", i=0):
    return {"type": "queue-operation", "operation": operation,
            "timestamp": _ts(i), "sessionId": SID, "content": content}


def bookkeeping_lines(leaf):
    """uuid-less lines the CLI appends that render nothing."""
    return [
        {"type": "last-prompt", "leafUuid": leaf, "sessionId": SID},
        {"type": "custom-title", "customTitle": "Demo session", "sessionId": SID},
        {"type": "file-history-snapshot", "messageId": leaf,
         "snapshot": {"trackedFileBackups": {}}, "isSnapshotUpdate": False},
        {"type": "file-history-delta", "messageId": leaf, "snapshotMessageId": leaf,
         "trackingPath": "a.py", "backup": {"version": 1}, "timestamp": _ts(59)},
    ]


def attachment(uuid, parent, i):
    return _envelope("attachment", uuid, parent, i,
                     attachment={"type": "environment", "snapshot": {"platform": "linux"}},
                     rendered=[{"content": "env"}])


# =============================================================================
# Fixture library: transcripts
# =============================================================================

# One realistic CLI 2.x turn plus a follow-up: queued prompt, attachment,
# thinking + text + Bash tool_use split across three lines of ONE API response,
# the tool_result with its toolUseResult sidecar, the final answer (a second
# API response), a stop-hook summary and the trailing bookkeeping lines.
USAGE_1 = usage(3, 40, cache_read=100)
USAGE_2 = usage(5, 12, cache_read=150, cache_create=7)


def fx_cli2_turn() -> List[Dict[str, Any]]:
    return [
        queue_op("enqueue", "List the files", 0),
        queue_op("dequeue", "", 0),
        user("u1", "List the files", i=1),
        attachment("att1", "u1", 1),
        *split_assistant(
            ["a1", "a2", "a3"],
            [thinking("Need ls."), text("I'll list them."),
             tool_use("toolu_1", "Bash", {"command": "ls", "description": "List files"})],
            parent="att1", i=2, msg_id="msg_1", use=USAGE_1,
        ),
        user_blocks("r1", [tool_result("toolu_1", "a.py\nb.py")], parent="a3", i=5,
                    tool_use_result={"stdout": "a.py\nb.py", "stderr": "",
                                     "interrupted": False, "isImage": False},
                    sourceToolAssistantUUID="a3"),
        assistant("a4", [text("Two files: a.py and b.py.")], parent="r1", i=6,
                  msg_id="msg_2", use=USAGE_2),
        system("s1", "stop_hook_summary", parent="a4", i=7, hookCount=2,
               hookInfos=[], hookErrors=[], preventedContinuation=False,
               stopReason="", hasOutput=False, toolUseID="toolu_stop"),
        *bookkeeping_lines("s1"),
    ]


def fx_legacy_task() -> List[Dict[str, Any]]:
    """SDK <=0.1.30: every block of a response in ONE entry, no message.id,
    ``Task`` sub-agent whose sidechain lines are inlined in the main file."""
    return [
        user("u1", "Research X", i=0),
        assistant("a1", [text("Delegating."),
                         tool_use("toolu_T", "Task", {"subagent_type": "researcher",
                                                      "description": "Research X",
                                                      "prompt": "Find X"})],
                  parent="u1", i=1, use=usage(10, 20)),
        user("sc1", "Find X", parent="a1", i=2, isSidechain=True),
        assistant("sc2", [text("X is 42")], parent="sc1", i=3, isSidechain=True,
                  use=usage(1000, 2000)),
        user_blocks("r1", [tool_result("toolu_T", [text("X is 42")])], parent="a1", i=4,
                    tool_use_result={"agentId": "agentabc",
                                     "content": [text("X is 42")], "totalTokens": 99}),
        assistant("a2", [text("Done: X is 42.")], parent="r1", i=5, use=usage(30, 40)),
    ]


def fx_agent_file_entries(agent_id="agentabc") -> List[Dict[str, Any]]:
    base = {"sessionId": SID, "agentId": agent_id, "isSidechain": True}
    return [
        {**user("g0", "Find X", i=0), **base},
        {**assistant("g1", [thinking("grep it"), tool_use("toolu_g", "Grep", {"pattern": "X"})],
                     parent="g0", i=1), **base},
        {**user_blocks("g2", [tool_result("toolu_g", "src/x.py:1: X = 42")], parent="g1", i=2), **base},
        {**assistant("g3", [text("X is 42")], parent="g2", i=3), **base},
    ]


NOTIFICATION = (
    "<task-notification>\n<task-id>ag1</task-id>\n<tool-use-id>toolu_A</tool-use-id>\n"
    "<status>completed</status>\n<summary>Tests passed</summary>\n"
    "<result>42 passed</result>\n<usage><subagent_tokens>1234</subagent_tokens>"
    "<tool_uses>7</tool_uses><duration_ms>5000</duration_ms></usage>\n"
    "</task-notification>"
)


def fx_agent_background() -> List[Dict[str, Any]]:
    """SDK >=0.1.48: ``Agent`` tool, backgrounded, launch-ACK then a terminal
    task-notification; plus a second launch the dispatcher rejected."""
    return [
        user("u1", "Run tests in background", i=0),
        assistant("a1", [tool_use("toolu_A", "Agent", {"subagent_type": "tester",
                                                       "description": "Run tests",
                                                       "prompt": "pytest -q",
                                                       "run_in_background": True})],
                  parent="u1", i=1, msg_id="msg_a1"),
        user_blocks("r1", [tool_result("toolu_A", "Async agent launched successfully. agentId: ag1")],
                    parent="a1", i=2),
        assistant("a2", [tool_use("toolu_B", "Agent", {"description": "Lint", "prompt": "ruff"})],
                  parent="r1", i=3, msg_id="msg_a2"),
        user_blocks("r2", [tool_result("toolu_B", '{"ok": false, "error": "no worker"}')],
                    parent="a2", i=4),
        assistant("a3", [text("Started the tests.")], parent="r2", i=5, msg_id="msg_a3"),
    ]


def fx_server_tools() -> List[Dict[str, Any]]:
    """SDK 0.1.65+: server-executed tools, one paired and one orphan result."""
    results = [{"type": "web_search_result", "url": "https://example.test", "title": "E"}]
    return [
        user("u1", "search the web", i=0),
        assistant("a1", [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
             "input": {"query": "q"}},
            {"type": "server_tool_result", "tool_use_id": "srvtoolu_1", "content": results},
            {"type": "server_tool_result", "tool_use_id": "srvtoolu_9", "content": "raw"},
            text("Found it."),
        ], parent="u1", i=1, msg_id="msg_s"),
    ]


def fx_compaction() -> List[Dict[str, Any]]:
    return [
        {"type": "summary", "summary": "Refactor session", "leafUuid": "a-old"},
        user("u1", "old prompt", i=0),
        assistant("a1", [text("old answer")], parent="u1", i=1, msg_id="m1"),
        system("cb", "compact_boundary", content="Conversation compacted", parent="a1", i=2,
               compactMetadata={"trigger": "auto", "preTokens": 155000}),
        system("mcb", "microcompact_boundary", content="Context microcompacted", parent="cb", i=3,
               microcompactMetadata={"trigger": "auto", "tokensSaved": 1200,
                                     "preTokens": 90000, "compactedToolIds": ["t1"]}),
        user("u2", "next prompt", parent="mcb", i=4),
        {"type": "summary", "summary": "Later summary", "leafUuid": "u2"},
    ]


def fx_system_mix() -> List[Dict[str, Any]]:
    return [
        system("h1", "thinking_tokens", content="1200", i=0),
        system("h2", "task_progress", content="agent step 3", i=1),
        system("s1", "informational", content="Hook ran", i=2),
        {"type": "hook_event", "uuid": "he1", "timestamp": _ts(3),
         "hook_event_name": "PreToolUse", "matcher": "Bash", "decision": "block",
         "reason": "rm -rf", "tool_name": "Bash", "session_id": SID},
        user("m1", "<local-command-caveat>Caveat: generated by local commands</local-command-caveat>",
             i=4, isMeta=True),
        user("c1", "<command-message>review</command-message>\n<command-name>/review</command-name>\n"
                   "<command-args>src</command-args>", i=5),
        user("l1", "<local-command-stdout>Context: 50% used</local-command-stdout>", i=6),
        assistant("e1", [text("API Error: 529 overloaded")], i=7, model="<synthetic>",
                  isApiErrorMessage=True),
        user("w1", "   \n  ", i=8),
        user_blocks("b1", [text("look at this"),
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": "iVBORw0KGgo="}},
                           text("<task-notification><tool-use-id>x</tool-use-id></task-notification>")],
                    i=9),
    ]


BIG_FILE = "".join(f"line {n}: some source text\n" for n in range(4000))  # ~100KB, not base64-like
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def fx_tool_pairing() -> List[Dict[str, Any]]:
    return [
        user("u1", "do things", i=0),
        *split_assistant(
            ["a1", "a2"],
            [tool_use("toolu_r", "Read", {"file_path": "/w/big.py"}),
             tool_use("toolu_e", "Bash", {"command": "false"})],
            parent="u1", i=1, msg_id="msg_p", use=usage(2, 2),
        ),
        # CLI 2.x writes one user line per result, each with its own sidecar
        # (an error's sidecar is the error string).
        user_blocks("r1", [tool_result("toolu_r", "(file)")], parent="a2", i=3,
                    tool_use_result={"type": "text", "file": {"filePath": "/w/big.py",
                                                              "content": BIG_FILE,
                                                              "numLines": 4000}}),
        user_blocks("r1e", [tool_result("toolu_e", "Exit code 1\nboom", is_error=True)],
                    parent="r1", i=3, tool_use_result="Error: Exit code 1\nboom"),
        assistant("a3", [tool_use("toolu_bg", "Bash", {"command": "sleep 999",
                                                       "run_in_background": True})],
                  parent="r1", i=4, msg_id="msg_bg"),
        user_blocks("r2", [tool_result("toolu_bg", "Command running in background with ID: bash_1")],
                    parent="a3", i=5),
        assistant("a4", [tool_use("toolu_big", "Grep", {"pattern": "x"})], parent="r2", i=6,
                  msg_id="msg_big"),
        user_blocks("r3", [tool_result("toolu_big", "y" * 300_000)], parent="a4", i=7),
        assistant("a5", [tool_use("toolu_shot", "mcp__browser__screenshot", {})], parent="r3",
                  i=8, msg_id="msg_shot"),
        user_blocks("r4", [tool_result("toolu_shot", [
            text("Screenshot taken"),
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
        ])], parent="a5", i=9),
        user_blocks("r5", [tool_result("toolu_zz", "result for a tool_use we never saw")],
                    parent="r4", i=10),
        assistant("a6", [tool_use("toolu_x", "Edit", {"file_path": "/w/a.py"})], parent="r5",
                  i=11, msg_id="msg_x"),
        # interrupted: no result for toolu_x
    ]


# ---- raw on-disk variants ----------------------------------------------------

def _line(e) -> bytes:
    return json.dumps(e).encode()


RAW_ONE = user("v1", "first", i=0)
RAW_TWO = assistant("v2", [text("second")], parent="v1", i=1)
RAW_THREE = user("v3", "third", parent="v2", i=2)

RAW_VARIANTS = {
    "clean": _line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n" + _line(RAW_THREE) + b"\n",
    "torn_last_line": (_line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n" + _line(RAW_THREE)[:40]),
    "invalid_middle": (_line(RAW_ONE) + b"\n{not json at all\n" + _line(RAW_TWO) + b"\n"
                       + _line(RAW_THREE) + b"\n"),
    "crlf": _line(RAW_ONE) + b"\r\n" + _line(RAW_TWO) + b"\r\n" + _line(RAW_THREE) + b"\r\n",
    "blank_lines": b"\n\n" + _line(RAW_ONE) + b"\n   \n\n" + _line(RAW_TWO) + b"\n" + _line(RAW_THREE),
    "nul_tail": (_line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n" + _line(RAW_THREE) + b"\n"
                 + b"\x00" * 64),
    "empty": b"",
}
RAW_EXPECTED = {
    "clean": ["v1", "v2", "v3"],
    "torn_last_line": ["v1", "v2"],
    "invalid_middle": ["v1", "v2", "v3"],
    "crlf": ["v1", "v2", "v3"],
    "blank_lines": ["v1", "v2", "v3"],
    "nul_tail": ["v1", "v2", "v3"],
    "empty": [],
}

# A line whose string value carries raw Latin-1 bytes (not valid UTF-8).
NON_UTF8_LINE = (b'{"type":"user","uuid":"v-bad","parentUuid":"v2","message":'
                 b'{"role":"user","content":"caf\xe9 cr\xe8me"}}')
NON_UTF8_FILE = (_line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n" + NON_UTF8_LINE + b"\n"
                 + _line(RAW_THREE) + b"\n")
NON_OBJECT_FILE = (_line(RAW_ONE) + b"\nnull\n42\n[1, 2]\n\"str\"\n" + _line(RAW_TWO) + b"\n"
                   + _line(RAW_THREE) + b"\n")


# =============================================================================
# Harness
# =============================================================================

@pytest.fixture(autouse=True)
def _fresh_parse_caches():
    """Every cache in jsonl_parser is process-global; start and end clean."""
    jp.invalidate_parse_cache()
    yield
    jp.invalidate_parse_cache()


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A private Claude projects root; returns the session's project dir."""
    root = tmp_path / "projects"
    pdir = root / jp.get_project_dir_name(WD)
    pdir.mkdir(parents=True)
    monkeypatch.setattr(jp, "settings", SimpleNamespace(get_claude_projects_dir=root))
    monkeypatch.setattr(jsonl_rewind_service, "claude_projects_dir", root)
    # Screenshots never resolve to a stored artifact unless a test says so.
    import app.core.artifact_store as artifact_store
    monkeypatch.setattr(artifact_store, "artifact_path", lambda digest: None)
    jp.invalidate_parse_cache()
    yield pdir
    jp.invalidate_parse_cache()


def write_entries(pdir: Path, entries, sid=SID, eol="\n") -> Path:
    path = pdir / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(e) + eol for e in entries), encoding="utf-8")
    return path


def write_raw(pdir: Path, data: bytes, sid=SID) -> Path:
    path = pdir / f"{sid}.jsonl"
    path.write_bytes(data)
    return path


def parse(sid=SID):
    jp.invalidate_parse_cache(sid)
    return jp.parse_session_history(sid, WD)


def shape(messages):
    """Compact (role, type, content-or-tool) view for exact ordering asserts."""
    out = []
    for m in messages:
        t = m.get("type")
        if t in ("tool_use",):
            out.append((m["role"], t, m["toolName"], m.get("toolStatus")))
        elif t == "subagent":
            out.append((m["role"], t, m["agentType"], m.get("agentStatus")))
        elif t == "system":
            out.append((m["role"], t, m["subtype"]))
        else:
            out.append((m["role"], t, m["content"]))
    return out


def by_tool(messages, tool_id):
    return next(m for m in messages if m.get("toolId") == tool_id)


# =============================================================================
# 1. Message extraction and ordering
# =============================================================================

class TestExtractionAndOrdering:
    def test_cli2_turn_renders_in_file_order(self, projects):
        write_entries(projects, fx_cli2_turn())
        messages, _ = parse()
        assert shape(messages) == [
            ("user", None, "List the files"),
            ("assistant", "thinking", "Need ls."),
            ("assistant", "text", "I'll list them."),
            ("assistant", "tool_use", "Bash", "complete"),
            ("assistant", "text", "Two files: a.py and b.py."),
            ("system", "system", "stop_hook_summary"),
        ]
        bash = by_tool(messages, "toolu_1")
        assert bash["id"] == "tool-toolu_1"
        assert bash["uuid"] == "a3"
        assert bash["toolResult"] == "a.py\nb.py"
        # every rendered message is anchored to its own JSONL line
        assert [m["uuid"] for m in messages] == ["u1", "a1", "a2", "a3", "a4", "s1"]
        assert messages[-1]["systemData"]["hookCount"] == 2
        assert messages[1]["metadata"]["model"] == MODEL

    def test_split_lines_count_each_api_response_once(self, projects):
        """Regression: CLI 2.x repeats a response's usage on every per-block
        line; the totals used to count msg_1 three times."""
        write_entries(projects, fx_cli2_turn())
        _, usage_data = parse()
        assert usage_data == {
            "total_tokens_in": 3 + 5,
            "total_tokens_out": 40 + 12,
            "cache_creation_tokens": 7,
            "cache_read_tokens": 150,
            "last_input_tokens": 5,
            "model": MODEL,
        }

    def test_session_cost_counts_split_lines_once(self, projects):
        """Same regression through get_session_cost_from_jsonl (session import)."""
        write_entries(projects, fx_cli2_turn())
        cost = jp.get_session_cost_from_jsonl(SID, WD)
        assert cost["total_tokens_in"] == 8
        assert cost["total_tokens_out"] == 52
        assert cost["cache_read_tokens"] == 150

    def test_repeated_message_id_takes_latest_usage_snapshot(self):
        entries = [
            assistant("x1", [thinking("t")], msg_id="m", use=usage(4, 10)),
            assistant("x2", [text("done")], parent="x1", msg_id="m", use=usage(4, 25)),
            assistant("x3", [text("no id")], parent="x2", use=usage(1, 1)),
        ]
        _, u = jp._transform_entries(entries, SID, WD)
        assert (u["total_tokens_in"], u["total_tokens_out"]) == (5, 26)

    def test_legacy_single_entry_multi_block_ids_are_unique(self, projects):
        write_entries(projects, fx_legacy_task())
        messages, usage_data = parse()
        ids = [m["id"] for m in messages]
        assert len(ids) == len(set(ids))
        assert shape(messages) == [
            ("user", None, "Research X"),
            ("assistant", "text", "Delegating."),
            ("assistant", "subagent", "researcher", "completed"),
            ("assistant", "text", "Done: X is 42."),
        ]
        # sidechain lines never render, but their usage is real spend: it
        # counts toward totals and NOT toward the context-window snapshot.
        assert usage_data["total_tokens_in"] == 10 + 1000 + 30
        assert usage_data["last_input_tokens"] == 30

    def test_file_order_wins_over_timestamps_and_parent_links(self, projects):
        """The parser linearises the FILE; it does not rebuild the parentUuid
        tree. A branch whose parent is an earlier node (the CLI's own rewind
        leaves one) and out-of-order timestamps both render in file order."""
        entries = [
            user("p1", "first", i=30),
            assistant("p2", [text("reply A")], parent="p1", i=31),
            user("p3", "abandoned branch", parent="p2", i=32),
            assistant("p4", [text("reply B")], parent="p1", i=5),  # re-parented, older ts
            user("p5", "orphan", parent="does-not-exist", i=1),
        ]
        write_entries(projects, entries)
        messages, _ = parse()
        assert [m["uuid"] for m in messages] == ["p1", "p2", "p3", "p4", "p5"]

    def test_bookkeeping_only_transcript_renders_nothing(self, projects):
        write_entries(projects, [queue_op("enqueue", "x"), attachment("at", None, 0),
                                 {"type": "summary", "summary": "s", "leafUuid": "z"},
                                 *bookkeeping_lines("z")])
        assert parse() == ([], {})

    def test_title_preview_and_count_ignore_system_meta_and_notifications(self, projects):
        path = write_entries(projects, fx_cli2_turn() + [user("n1", NOTIFICATION, i=9)])
        title, preview, count = jp._get_session_title_and_preview(path)
        assert title == "List the files"
        assert preview == "I'll list them."
        # u1 + 4 assistant lines; tool_result / system / notification not counted
        assert count == 5


# =============================================================================
# 2. uuid / parent chain handling
# =============================================================================

class TestUuidHandling:
    def test_missing_uuids_get_unique_counter_ids(self):
        entries = [
            {"type": "user", "message": {"role": "user", "content": "one"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "two"}},
            {"type": "user", "message": {"role": "user", "content": "three"}},
        ]
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert [m["id"] for m in messages] == ["msg-0", "text-msg-1", "msg-2"]

    def test_null_uuid_falls_back_like_a_missing_one(self):
        """Regression: ``"uuid": null`` used to become id/uuid None for every
        such entry, so two of them collided."""
        entries = [
            {"type": "user", "uuid": None, "message": {"role": "user", "content": "one"}},
            {"type": "user", "uuid": None, "message": {"role": "user", "content": "two"}},
        ]
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert [m["id"] for m in messages] == ["msg-0", "msg-1"]
        assert all(m["uuid"] for m in messages)

    def test_ids_are_stable_across_reparse(self, projects):
        write_entries(projects, fx_cli2_turn() + fx_tool_pairing())
        first, _ = parse()
        second, _ = parse()
        assert [m["id"] for m in first] == [m["id"] for m in second]

    def test_rewind_slice_ignores_uuidless_lines_and_parent_links(self):
        entries = fx_cli2_turn() + [
            queue_op("enqueue", "next", 20), queue_op("dequeue", "", 20),
            user("u2", "next", parent="does-not-exist", i=20),
            assistant("a5", [text("ok")], parent="u2", i=21),
        ]
        keep, err = compute_rewind_keep_count(entries, "u2", False, "user")
        assert err is None
        # cut before the stranded queue-ops that belong to the removed prompt
        assert entries[keep - 1]["type"] == "file-history-delta"
        assert all(e.get("uuid") != "u2" for e in entries[:keep])
        _, err = compute_rewind_keep_count(entries, "nope", False, "user")
        assert err == "Message with UUID nope not found"


# =============================================================================
# 3. Hidden-subtype filter and system mix
# =============================================================================

class TestSystemMessages:
    def test_hidden_subtypes_dropped_on_reload(self, projects):
        write_entries(projects, fx_system_mix())
        messages, _ = parse()
        subtypes = [m.get("subtype") for m in messages if m["type"] == "system"]
        assert "thinking_tokens" not in subtypes
        assert "task_progress" not in subtypes
        assert subtypes[0] == "informational"

    def test_system_mix_exact_rendering(self, projects):
        write_entries(projects, fx_system_mix())
        messages, _ = parse()
        assert shape(messages) == [
            ("system", "system", "informational"),
            ("system", "system", "hook_event"),
            ("user", "command", "/review src"),
            ("system", "system", "local_command"),
            ("system", "system", "api_error"),
            ("user", None, "look at this"),
        ]
        hook = messages[1]
        assert hook["systemData"]["decision"] == "block"
        assert hook["systemData"]["tool_name"] == "Bash"
        cmd = messages[2]
        assert (cmd["commandName"], cmd["commandId"], cmd["commandArgs"]) == ("/review", "review", "src")
        assert messages[3]["content"] == "Context: 50% used"
        assert messages[4]["content"] == "API Error: 529 overloaded"

    def test_compaction_boundaries_normalised_and_summaries_dropped(self, projects):
        write_entries(projects, fx_compaction())
        messages, _ = parse()
        assert shape(messages) == [
            ("user", None, "old prompt"),
            ("assistant", "text", "old answer"),
            ("system", "system", "compact_boundary"),
            ("system", "system", "microcompact_boundary"),
            ("user", None, "next prompt"),
        ]
        cb, mcb = messages[2], messages[3]
        assert cb["systemData"]["compact_metadata"] == {"trigger": "auto", "pre_tokens": 155000}
        assert cb["metadata"]["compactMetadata"] == {"trigger": "auto", "preTokens": 155000}
        assert mcb["systemData"]["microcompactMetadata"] == {
            "trigger": "auto", "tokensSaved": 1200, "preTokens": 90000,
        }

    def test_backfill_conversion_also_drops_hidden_subtypes(self):
        msgs = [
            {"role": "system", "type": "system", "subtype": "thinking_tokens", "content": "9"},
            {"role": "system", "type": "system", "systemSubtype": "task_progress", "content": "p"},
            {"role": "system", "type": "system", "subtype": "informational", "content": "shown"},
        ]
        events = hb.convert_messages_to_events("sess-x", msgs)
        assert [(e["type"], e["payload"]["subtype"]) for e in events] == [
            ("system.note", "informational"),
        ]


# =============================================================================
# 4. Thinking blocks
# =============================================================================

class TestThinking:
    def test_thinking_rendered_before_text_and_empty_blocks_skipped(self):
        entries = [
            user("u1", "q"),
            assistant("a1", [thinking(""), {"type": "redacted_thinking", "data": "abc"},
                             thinking("step one"), text("   "), text("answer")],
                      parent="u1", msg_id="m"),
        ]
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert shape(messages) == [
            ("user", None, "q"),
            ("assistant", "thinking", "step one"),
            ("assistant", "text", "answer"),
        ]
        assert messages[1]["id"].startswith("thinking-a1-")
        assert messages[1]["metadata"]["model"] == MODEL

    def test_agent_children_carry_no_thinking(self):
        children = jp.transform_agent_entries(fx_agent_file_entries())
        assert [(c["type"], c.get("toolName") or c.get("content")) for c in children] == [
            ("tool_use", "Grep"),
            ("text", "X is 42"),
        ]


# =============================================================================
# 5. Tool pairing
# =============================================================================

class TestToolPairing:
    def test_pairing_matrix(self, projects):
        write_entries(projects, fx_tool_pairing())
        messages, _ = parse()
        read = by_tool(messages, "toolu_r")
        assert read["toolStatus"] == "complete"
        assert read["toolResult"] == ("File: /w/big.py\n" + BIG_FILE)[:2000]
        err = by_tool(messages, "toolu_e")
        assert err["toolStatus"] == "error"
        assert err["toolResult"] == "Error: Exit code 1\nboom"
        assert by_tool(messages, "toolu_bg")["toolStatus"] == "running"
        big = by_tool(messages, "toolu_big")
        assert big["toolResult"] == "y" * 2000
        shot = by_tool(messages, "toolu_shot")
        assert shot["toolResult"] == "Screenshot taken"
        assert shot["toolScreenshot"] == f"data:image/png;base64,{PNG_B64}"
        interrupted = by_tool(messages, "toolu_x")
        assert interrupted["toolStatus"] == "running"
        assert "toolResult" not in interrupted
        orphan = by_tool(messages, "toolu_zz")
        assert orphan["type"] == "tool_result"
        assert orphan["id"] == "result-r5-toolu_zz"
        assert orphan["toolName"] is None
        assert orphan["content"] == "result for a tool_use we never saw"
        # results merged into their cards — no stray tool_result rows besides the orphan
        assert [m["toolId"] for m in messages if m["type"] == "tool_result"] == ["toolu_zz"]

    def test_background_bash_ack_keeps_card_running(self, projects):
        entries = fx_tool_pairing()[5:7]  # a3 + r2 only
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert messages[0]["toolStatus"] == "running"
        assert messages[0]["toolResult"] == "Command running in background with ID: bash_1"

    def test_parallel_results_in_one_sdk_entry_pair_by_id(self):
        """SDK-streamed shape: several tool_result blocks in ONE user entry,
        no sidecar — each block pairs with its own card, in either order."""
        entries = [
            assistant("a", [tool_use("t1", "Glob", {}), tool_use("t2", "Grep", {})], msg_id="m"),
            user_blocks("r", [tool_result("t2", "two"), tool_result("t1", [text("one")])], parent="a"),
        ]
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert [(m["toolName"], m["toolStatus"], m["toolResult"]) for m in messages] == [
            ("Glob", "complete", "one"), ("Grep", "complete", "two"),
        ]

    def test_screenshot_resolves_to_artifact_url_when_stored(self, projects, monkeypatch):
        import app.core.artifact_store as artifact_store
        monkeypatch.setattr(artifact_store, "artifact_path", lambda digest: Path("/x") / digest)
        from app.core.artifact_ingest import content_hash
        digest = content_hash(base64.b64decode(PNG_B64))
        messages, _ = jp._transform_entries(fx_tool_pairing()[9:11], SID, WD)
        assert messages[0]["toolScreenshot"] == f"/api/v1/artifacts/{digest}"

    def test_read_of_binary_file_is_summarised(self):
        blob = "iVBOR" + "A" * 40_000
        entries = [
            assistant("a", [tool_use("t", "Read", {"file_path": "/w/i.png"})]),
            user_blocks("r", [tool_result("t", "(img)")], parent="a",
                        tool_use_result={"type": "text", "file": {"filePath": "/w/i.png",
                                                                  "content": blob}}),
        ]
        messages, _ = jp._transform_entries(entries, SID, WD)
        assert messages[0]["toolResult"] == (
            "File: /w/i.png\n[Image/binary content - 29KB - truncated for display]"
        )

    def test_server_tools_paired_and_orphan(self, projects):
        write_entries(projects, fx_server_tools())
        messages, _ = parse()
        assert shape(messages) == [
            ("user", None, "search the web"),
            ("assistant", "tool_use", "web_search", "complete"),
            ("assistant", "tool_use", "server_tool", "complete"),
            ("assistant", "text", "Found it."),
        ]
        paired = messages[1]
        assert paired["serverSide"] is True
        assert json.loads(paired["toolResult"]) == [
            {"type": "web_search_result", "url": "https://example.test", "title": "E"}
        ]
        assert messages[2]["toolResult"] == "raw"
        assert messages[2]["toolId"] == "srvtoolu_9"

    def test_legacy_task_subagent_gets_flat_layout_children(self, projects):
        write_entries(projects, fx_legacy_task())
        (projects / "agent-agentabc.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in fx_agent_file_entries()))
        messages, _ = parse()
        sub = messages[2]
        assert sub["type"] == "subagent"
        assert sub["id"] == "subagent-toolu_T"
        assert sub["agentStatus"] == "completed"
        assert sub["agentId"] == "agentabc"
        assert sub["content"] == "X is 42"
        assert [(c["type"], c.get("toolStatus")) for c in sub["agentChildren"]] == [
            ("tool_use", "complete"), ("text", None),
        ]
        assert sub["agentChildren"][0]["toolResult"] == "src/x.py:1: X = 42"

    def test_agent_nested_layout_children_ignore_foreign_session_files(self, projects):
        entries = [
            user("u1", "go", i=0),
            assistant("a1", [tool_use("toolu_N", "Agent", {"description": "Look", "prompt": "p"})],
                      parent="u1", i=1),
            user_blocks("r1", [tool_result("toolu_N", "done")], parent="a1", i=2,
                        tool_use_result={"agentId": "nest1"}),
        ]
        write_entries(projects, entries)
        nested = projects / SID / "subagents"
        nested.mkdir(parents=True)
        (nested / "agent-nest1.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in fx_agent_file_entries("nest1")))
        (projects / "agent-nest1.jsonl").write_text(
            json.dumps({"sessionId": "someone-else", "type": "user"}) + "\n")
        messages, _ = parse()
        sub = messages[1]
        assert sub["agentType"] == "Look"
        assert sub["agentId"] == "nest1"
        assert len(sub["agentChildren"]) == 2

    def test_background_agent_ack_then_task_notification(self, projects):
        write_entries(projects, fx_agent_background())
        messages, _ = parse()
        a = by_tool(messages, "toolu_A")
        assert a["agentStatus"] == "running"  # launch-ACK only
        write_entries(projects, fx_agent_background() + [user("n1", NOTIFICATION, parent="a3", i=9)])
        messages, _ = parse()
        a = by_tool(messages, "toolu_A")
        assert a["agentStatus"] == "completed"
        assert a["agentUsage"] == {"total_tokens": 1234, "tool_uses": 7, "duration_ms": 5000}
        assert a["content"] == "42 passed"
        # the notification itself never renders
        assert all("<task-notification>" not in str(m.get("content")) for m in messages)

    def test_rejected_agent_launch_is_error(self, projects):
        write_entries(projects, fx_agent_background())
        messages, _ = parse()
        assert by_tool(messages, "toolu_B")["agentStatus"] == "error"


# =============================================================================
# 6. Parse caches and windowed reads
# =============================================================================

class TestParseCache:
    def test_cache_hit_returns_private_copy_and_file_change_invalidates(self, projects, monkeypatch):
        path = write_entries(projects, fx_cli2_turn())
        calls = []
        real = jp._parse_session_history_uncached
        monkeypatch.setattr(jp, "_parse_session_history_uncached",
                            lambda *a: calls.append(a[0]) or real(*a))
        first, _ = jp.parse_session_history(SID, WD)
        first[0]["content"] = "MUTATED"
        first.append({"junk": True})
        second, _ = jp.parse_session_history(SID, WD)
        assert calls == [SID]
        assert second[0]["content"] == "List the files"
        assert len(second) == 6
        with path.open("a") as f:
            f.write(json.dumps(user("u9", "more", i=40)) + "\n")
        third, _ = jp.parse_session_history(SID, WD)
        assert calls == [SID, SID]
        assert third[-1]["content"] == "more"

    def test_oversized_transcript_is_not_cached(self, projects, monkeypatch):
        entries = [user("u1", "q"), assistant("a1", [tool_use("t", "Write", {"content": "z" * 20_000})],
                                              parent="u1")]
        write_entries(projects, entries)
        monkeypatch.setattr(jp, "_parse_cache", jp.ByteBoundedLRU(50, 100_000, 10_000))
        calls = []
        real = jp._parse_session_history_uncached
        monkeypatch.setattr(jp, "_parse_session_history_uncached",
                            lambda *a: calls.append(1) or real(*a))
        jp.parse_session_history(SID, WD)
        jp.parse_session_history(SID, WD)
        assert len(calls) == 2
        assert SID not in jp._parse_cache

    def test_pre_seed_serves_snapshot_until_the_file_changes(self, projects):
        path = write_entries(projects, fx_cli2_turn())
        seeded = [{"id": "seed", "role": "assistant", "type": "text", "content": "seeded"}]
        assert jp.pre_seed_parse_cache(SID, seeded, {"total_tokens_in": 1}, WD,
                                       store_version_loader=lambda s: (3, 30)) is True
        seeded[0]["content"] = "caller mutates its own list"
        messages, u = jp.parse_session_history(SID, WD)
        assert messages == [{"id": "seed", "role": "assistant", "type": "text", "content": "seeded"}]
        assert u == {"total_tokens_in": 1}
        with path.open("a") as f:
            f.write(json.dumps(user("u9", "more", i=40)) + "\n")
        messages, _ = jp.parse_session_history(SID, WD)
        assert messages[0]["content"] == "List the files"

    def test_pre_seed_without_jsonl_seeds_only_the_store_namespace(self, projects):
        other = "0f0e0d0c-0b0a-4000-8000-000000000001"
        assert jp.pre_seed_parse_cache(other, [{"id": "x"}], {}, WD,
                                       store_version_loader=lambda s: (1, 1)) is True
        assert other in jp._store_parse_cache
        assert other not in jp._parse_cache

    def test_pre_seed_drops_store_entry_when_store_moved_on(self, projects):
        write_entries(projects, fx_cli2_turn())
        jp._store_parse_cache[SID] = ([{"id": "old"}], {}, (1, 1))
        jp.pre_seed_parse_cache(SID, [{"id": "new"}], {}, WD,
                                store_version_loader=lambda s: (5, 50),
                                expected_store_version=(4, 40))
        assert SID not in jp._store_parse_cache
        jp.pre_seed_parse_cache(SID, [{"id": "new"}], {}, WD,
                                store_version_loader=lambda s: (5, 50),
                                expected_store_version=(5, 50))
        assert jp._store_parse_cache.get(SID)[2] == (5, 50)

    def test_store_parse_cache_revalidates_on_version(self):
        entries = fx_cli2_turn()
        loads = []
        version = [(len(entries), 100)]

        def loader(sid, subpath=None):
            loads.append(subpath)
            return list(entries) if subpath is None else []

        a, _ = jp.parse_session_history_from_store(SID, WD, loader,
                                                   store_version_loader=lambda s: version[0])
        b, _ = jp.parse_session_history_from_store(SID, WD, loader,
                                                   store_version_loader=lambda s: version[0])
        assert loads == [None]
        assert a == b
        version[0] = (len(entries) - 2, 101)  # a rewind elsewhere deleted rows
        jp.parse_session_history_from_store(SID, WD, loader,
                                            store_version_loader=lambda s: version[0])
        assert loads == [None, None]

    def test_byte_bounded_lru_contract(self):
        lru = jp.ByteBoundedLRU(max_entries=3, max_bytes=1000, max_entry_bytes=600)
        assert lru.put("a", "x", size=400) and lru.put("b", "y", size=400)
        lru.get("a")  # refresh a
        lru.put("c", "z", size=400)  # over budget: evicts LRU = b
        assert lru.keys() == ["a", "c"] and lru.current_bytes == 800
        assert lru.put("a", "huge", size=700) is False  # refused AND old value gone
        assert "a" not in lru and lru.current_bytes == 400
        with pytest.raises(KeyError):
            del lru["missing"]
        with pytest.raises(KeyError):
            lru["missing"]
        del lru["c"]
        assert len(lru) == 0 and lru.current_bytes == 0


def _window_loader_over(entries, context):
    """Mimics pg_session_store._load_entries_window_by_session_id_sync."""
    def _loader(sid, subpath=None, *, limit, before_seq=None):
        total = len(entries)
        end = total if before_seq is None else max(0, min(before_seq, total))
        start = max(0, end - limit)
        load_start = max(0, start - context)
        load_end = min(total, end + context)
        return {"entries": entries[load_start:load_end], "total": total,
                "start": load_start, "window_start": start, "window_end": end}
    return _loader


def _full_slice(entries, start, end):
    bounds: List[int] = []
    msgs, _ = jp._transform_entries(list(entries), SID, WD, entry_message_boundaries=bounds)
    return msgs[jp._boundary(bounds, start, len(msgs)):jp._boundary(bounds, end, len(msgs))]


def _strip_ids(msgs):
    return [{k: v for k, v in m.items() if k != "id"} for m in msgs]


class TestWindowedReads:
    def _many_turns(self, n):
        out = []
        for k in range(n):
            out += [queue_op("enqueue", f"p{k}", k), queue_op("dequeue", "", k),
                    user(f"u{k}", f"prompt {k}", i=k),
                    assistant(f"a{k}", [text(f"answer {k}")], parent=f"u{k}", i=k, msg_id=f"m{k}")]
        return out

    def test_bookkeeping_lines_do_not_force_a_full_parse(self):
        """Regression: queue-operation lines carry no uuid, and the window
        safety check refused ANY uuid-less entry — so every real transcript
        fell back to reading the whole store."""
        entries = self._many_turns(10)  # 40 entries
        full_calls = []

        def full_loader(sid, subpath=None):
            full_calls.append(subpath)
            return list(entries) if subpath is None else []

        msgs, _, page = jp.parse_session_window_from_store(
            SID, WD, _window_loader_over(entries, 2), full_loader,
            limit=8, use_cache=False)
        assert full_calls == []
        assert _strip_ids(msgs) == _strip_ids(_full_slice(entries, 32, 40))
        assert [m["content"] for m in msgs] == ["prompt 8", "answer 8", "prompt 9", "answer 9"]
        assert page["first_seq"] == 32 and page["next_before_seq"] == 32

    def test_uuidless_message_entry_still_forces_full_parse(self):
        entries = self._many_turns(4)
        del entries[-2]["uuid"]  # a message-producing entry without identity
        full_calls = []

        def full_loader(sid, subpath=None):
            full_calls.append(subpath)
            return list(entries) if subpath is None else []

        msgs, _, _ = jp.parse_session_window_from_store(
            SID, WD, _window_loader_over(entries, 1), full_loader, limit=4, use_cache=False)
        assert full_calls == [None]
        assert msgs == _full_slice(entries, 12, 16)

    def test_result_beyond_merge_context_falls_back_to_full_parse(self):
        entries = [
            user("u0", "start", i=0),
            assistant("a0", [tool_use("toolu_far", "Bash", {"command": "make"})], parent="u0", i=1),
            user("u1", "meanwhile", i=2), assistant("a1", [text("ok")], i=3),
            user("u2", "still", i=4), assistant("a2", [text("ok")], i=5),
            user_blocks("rf", [tool_result("toolu_far", "built")], i=6),
            assistant("a3", [text("built!")], i=7),
        ]

        def full_loader(sid, subpath=None):
            return list(entries) if subpath is None else []

        msgs, _, page = jp.parse_session_window_from_store(
            SID, WD, _window_loader_over(entries, 1), full_loader,
            limit=2, before_seq=2, use_cache=False)
        card = by_tool(msgs, "toolu_far")
        assert card["toolStatus"] == "complete"
        assert card["toolResult"] == "built"
        assert page["first_seq"] == 0 and page["has_more"] is False

    def test_window_cache_is_separate_from_full_read_and_invalidated_together(self):
        entries = self._many_turns(3)
        loader = _window_loader_over(entries, 0)

        def full_loader(sid, subpath=None):
            return list(entries) if subpath is None else []

        jp.parse_session_window_from_store(SID, WD, loader, full_loader, limit=4,
                                           store_version_loader=lambda s: (12, 12))
        key = jp._window_cache_key(SID, 4, None)
        assert key in jp._store_parse_cache and SID not in jp._store_parse_cache
        jp.invalidate_parse_cache(SID)
        assert key not in jp._store_parse_cache


# =============================================================================
# 7. Rewind: JSONL truncation + DB anchor resolution / delete-after
# =============================================================================

def _uuids_on_disk(path: Path) -> List[Optional[str]]:
    out = []
    for raw in path.read_bytes().splitlines():
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            entry = None
        out.append(entry.get("uuid") if isinstance(entry, dict) else "<unparsed>")
    return out


def fx_two_turns_with_queue() -> List[Dict[str, Any]]:
    return fx_cli2_turn() + [
        queue_op("enqueue", "and tests?", 20), queue_op("dequeue", "", 20),
        user("u2", "and tests?", parent="s1", i=21),
        assistant("a5", [text("Done.")], parent="u2", i=22, msg_id="msg_5"),
    ]


class TestJsonlRewind:
    def test_checkpoints_from_realistic_transcript(self):
        entries = fx_two_turns_with_queue() + [
            e for e in fx_system_mix() if e.get("uuid") not in ("w1", "b1")
        ] + [user_blocks("b2", [text("see"), text("this")], i=30)]
        cps = checkpoints_from_entries(entries)
        assert [(c.uuid, c.role, c.full_message) for c in cps] == [
            ("u1", "user", "List the files"),
            ("a2", "assistant", "I'll list them."),
            ("a4", "assistant", "Two files: a.py and b.py."),
            ("u2", "user", "and tests?"),
            ("a5", "assistant", "Done."),
            ("e1", "assistant", "API Error: 529 overloaded"),
            ("b2", "user", "see this"),
        ]
        assert [c.index for c in cps] == list(range(7))

    def test_user_target_drops_prompt_and_its_queue_ops(self, projects):
        path = write_entries(projects, fx_two_turns_with_queue())
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "u2", WD, include_response=False)
        assert res.success, res
        assert res.remaining_user_turns == 1
        on_disk = _uuids_on_disk(path)
        assert on_disk[-1] is None  # the file-history-delta bookkeeping line
        assert "u2" not in on_disk
        types = [json.loads(line)["type"] for line in path.read_text().splitlines()]
        assert types[-4:] != ["queue-operation"] * 4
        assert types.count("queue-operation") == 2  # only the FIRST prompt's pair

    def test_assistant_target_keeps_rest_of_turn(self, projects):
        path = write_entries(projects, fx_two_turns_with_queue())
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "a2", WD, include_response=True,
                                                          target_role="assistant")
        assert res.success and res.messages_removed == 4
        on_disk = _uuids_on_disk(path)
        assert on_disk[:4] == [None, None, "u1", "att1"]
        assert "a3" in on_disk and "r1" in on_disk and "a4" in on_disk
        assert "u2" not in on_disk and "a5" not in on_disk

    def test_tail_target_is_a_noop(self, projects):
        path = write_entries(projects, fx_two_turns_with_queue())
        before = path.read_bytes()
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "a5", WD, target_role="assistant")
        assert res.success and res.messages_removed == 0
        assert res.message == "Already at the target checkpoint"
        assert path.read_bytes() == before

    def test_non_utf8_bytes_survive_truncation_byte_for_byte(self, projects):
        """Regression: one non-UTF-8 byte made the whole file unreadable to
        the rewind service ("JSONL file is empty"); kept lines must also be
        written back exactly."""
        path = write_raw(projects, NON_UTF8_FILE)
        cps = jsonl_rewind_service.get_checkpoints(SID, WD)
        assert [c.uuid for c in cps] == ["v1", "v2", "v-bad", "v3"]
        assert cps[2].full_message == "caf� cr�me"
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "v3", WD, include_response=False)
        assert res.success, res
        expected = _line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n" + NON_UTF8_LINE + b"\n"
        assert path.read_bytes() == expected

    def test_non_object_and_invalid_lines_are_preserved_verbatim(self, projects):
        """Regression: a valid-JSON non-object line (``null``) raised out of
        the reader, hiding every later line from the rewind."""
        path = write_raw(projects, NON_OBJECT_FILE)
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "v3", WD, include_response=False)
        assert res.success, res
        assert path.read_bytes() == (_line(RAW_ONE) + b"\nnull\n42\n[1, 2]\n\"str\"\n"
                                     + _line(RAW_TWO) + b"\n")

    def test_crlf_and_torn_tail_rewind(self, projects):
        path = write_raw(projects, RAW_VARIANTS["crlf"])
        assert [c.uuid for c in jsonl_rewind_service.get_checkpoints(SID, WD)] == ["v1", "v2", "v3"]
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "v3", WD, include_response=False)
        assert res.success
        assert path.read_bytes() == _line(RAW_ONE) + b"\n" + _line(RAW_TWO) + b"\n"
        write_raw(projects, RAW_VARIANTS["torn_last_line"])
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "v1", WD, include_response=False)
        assert res.success and res.messages_removed == 3  # incl. the torn line

    @pytest.mark.parametrize("setup, message", [
        ("missing", "JSONL file not found"),
        ("empty", "JSONL file is empty"),
        ("unknown_target", "Target checkpoint not found"),
    ])
    def test_rewind_error_paths(self, projects, setup, message):
        if setup == "empty":
            write_raw(projects, b"\n\n")
        elif setup == "unknown_target":
            write_raw(projects, RAW_VARIANTS["clean"])
        res = jsonl_rewind_service.truncate_to_checkpoint(SID, "zzz", WD)
        assert (res.success, res.message) == (False, message)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
    def test_rewind_in_read_only_dir_fails_cleanly(self, projects):
        path = write_raw(projects, RAW_VARIANTS["clean"])
        before = path.read_bytes()
        projects.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            res = jsonl_rewind_service.truncate_to_checkpoint(SID, "v3", WD, include_response=False)
        finally:
            projects.chmod(stat.S_IRWXU)
        assert res.success is False
        assert res.message == "Failed to truncate JSONL file"
        assert path.read_bytes() == before
        assert [p.name for p in projects.iterdir()] == [path.name]  # no temp file left


APP_SID = "app-sess-corpus"


@pytest.fixture
def db_session(mock_db):
    sessions_db.create_session(APP_SID, "default")
    return APP_SID


def _rows(sid=APP_SID):
    return [(r["role"], r["content"]) for r in database.get_session_messages(sid)]


def _add(role, content, **kw):
    return database.add_session_message(APP_SID, role, content, **kw)["id"]


class TestRewindDbSync:
    def _transcript(self):
        return [
            user("u1", "fix the bug", i=0),
            assistant("a1", [text("Done.")], parent="u1", i=1),
            user("u2", "add tests", parent="a1", i=2),
            assistant("a2", [text("Done.")], parent="u2", i=3),
            user("u3", "ship it", parent="a2", i=4),
            assistant("a3", [text("Shipped.")], parent="u3", i=5),
        ]

    def _db(self):
        return [_add("user", "fix the bug"), _add("assistant", "Done."),
                _add("user", "add tests"), _add("assistant", "Done."),
                _add("user", "ship it"), _add("assistant", "Shipped.")]

    def test_backfill_uuids_from_disk_is_idempotent(self, projects, db_session):
        write_entries(projects, self._transcript())
        ids = self._db()
        assert rds.backfill_user_jsonl_uuids(APP_SID, SID, WD) == 3
        rows = {r["id"]: r["jsonl_uuid"] for r in database.get_session_messages(APP_SID)}
        assert [rows[i] for i in ids] == ["u1", None, "u2", None, "u3", None]
        assert rds.backfill_user_jsonl_uuids(APP_SID, SID, WD) == 0

    def test_backfill_stops_at_first_drift(self, projects, db_session):
        write_entries(projects, self._transcript())
        _add("user", "fix the bug")
        _add("user", "something the transcript never saw")
        _add("user", "ship it")
        assert rds.backfill_user_jsonl_uuids(APP_SID, SID, WD) == 1

    def test_user_anchor_fast_path_and_fallback(self, projects, db_session):
        write_entries(projects, self._transcript())
        ids = self._db()
        # resolve_rewind_anchor backfills first, then the uuid lookup hits
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "u2") == ids[2]
        # with the backfill unable to run, position+content fallback agrees
        database.set_message_jsonl_uuid(ids[2], None)
        assert rds._fallback_anchor_by_content(APP_SID, SID, WD, "u2") == ids[2]

    def test_assistant_anchor_with_repeated_text_picks_the_right_occurrence(self, projects, db_session):
        """Regression: both replies are "Done."; the fallback always took the
        LAST match, anchoring a rewind to a1 on a2 and keeping turn 2."""
        write_entries(projects, self._transcript())
        ids = self._db()
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "a1") == ids[1]
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "a2") == ids[3]
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "a3") == ids[5]

    def test_assistant_occurrence_counts_from_the_end(self, projects, db_session):
        """Older DB rows the transcript no longer holds must not shift it."""
        write_entries(projects, self._transcript()[2:])  # turn 1 gone from JSONL
        ids = self._db()
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "a2") == ids[3]

    def test_assistant_anchor_refused_when_db_lacks_later_matches(self, projects, db_session):
        """A session spanning the cutover: modern turns write no assistant rows,
        so the DB holds fewer "Done." rows than the transcript. Counting from
        the end would walk past a2's own row to a1 and the include_response
        delete would then drop turn 2, which the rewind keeps. Skip the DB sync
        instead."""
        transcript = self._transcript()
        transcript[5] = assistant("a3", [text("Done.")], parent="u3", i=5)
        write_entries(projects, transcript)
        ids = [_add("user", "fix the bug"), _add("assistant", "Done."),
               _add("user", "add tests"), _add("assistant", "Done."),
               _add("user", "ship it")]  # modern turn 3: no assistant row

        anchor = rds.resolve_rewind_anchor(APP_SID, SID, WD, "a2")

        assert anchor is None
        assert anchor != ids[1]

    def test_assistant_anchor_refused_when_an_old_row_offsets_a_missing_one(self, projects, db_session):
        """R8-8: the totals guard balanced out. Transcript a1/a2/a3 are all
        "Done." and modern a3 wrote no row, while the DB still holds an older
        "Done." the transcript no longer has: three matches on each side. The
        from-the-end pick for a2 then landed on a1's row, and the
        include_response delete dropped turn 2, which the rewind keeps."""
        transcript = self._transcript()
        transcript[5] = assistant("a3", [text("Done.")], parent="u3", i=5)
        write_entries(projects, transcript)
        ids = [_add("user", "an older prompt"), _add("assistant", "Done."),
               _add("user", "fix the bug"), _add("assistant", "Done."),
               _add("user", "add tests"), _add("assistant", "Done."),
               _add("user", "ship it")]  # modern turn 3: no assistant row

        anchor = rds.resolve_rewind_anchor(APP_SID, SID, WD, "a2")

        assert anchor is None
        assert anchor != ids[3]

    def test_content_mismatch_refuses_anchor(self, projects, db_session):
        write_entries(projects, self._transcript())
        _add("user", "fix the bug")
        _add("assistant", "Done.")
        _add("user", "COMPLETELY DIFFERENT")
        # backfill stops at the drift, uuid lookup misses, fallback refuses
        assert rds.resolve_rewind_anchor(APP_SID, SID, WD, "u2") is None

    def test_assistant_target_delete_mirrors_jsonl_cut(self, projects, db_session):
        """Regression: include_response=True (what the UI sends for every
        assistant target) widened to the NEXT turn's assistant row, so the
        next turn's prompt and reply survived the rewind."""
        write_entries(projects, self._transcript())
        ids = self._db()
        anchor = rds.resolve_rewind_anchor(APP_SID, SID, WD, "a1")
        assert rds.apply_rewind_delete(APP_SID, anchor, include_response=True) == 4
        assert _rows() == [("user", "fix the bug"), ("assistant", "Done.")]
        assert ids[1] == anchor

    def test_user_target_keep_response_keeps_whole_turn(self, db_session):
        ids = [_add("user", "q1"), _add("assistant", "looking"),
               _add("tool_result", "out", metadata={"tool_id": "t"}),
               _add("assistant", "answer"), _add("command", "/compact"),
               _add("assistant", "compacted")]
        assert rds.apply_rewind_delete(APP_SID, ids[0], include_response=True) == 2
        assert _rows() == [("user", "q1"), ("assistant", "looking"),
                           ("tool_result", "out"), ("assistant", "answer")]

    def test_user_target_drop_and_tail_noop_and_purge(self, db_session):
        ids = [_add("user", "q1"), _add("assistant", "a1"), _add("user", "q2"), _add("assistant", "a2")]
        assert rds.apply_rewind_delete(APP_SID, ids[3], include_response=True) == 0
        assert rds.apply_rewind_delete(APP_SID, ids[2], include_response=False) == 2
        assert _rows() == [("user", "q1"), ("assistant", "a1")]
        assert rds.delete_all_session_rows(APP_SID) == 2
        assert _rows() == []

    def test_no_sdk_session_short_circuits(self, db_session):
        assert rds.backfill_user_jsonl_uuids(APP_SID, None, WD) == 0
        assert rds.resolve_rewind_anchor(APP_SID, None, WD, "u1") is None


# =============================================================================
# 8. History backfill into the event store
# =============================================================================

class TestHistoryBackfill:
    @pytest.fixture
    def seeded(self, projects, mock_db, monkeypatch):
        sessions_db.create_session(APP_SID, "default")
        sessions_db.update_session(APP_SID, sdk_session_id=SID)
        path = write_entries(projects, fx_cli2_turn() + fx_tool_pairing()[:5])

        def via_store(sdk, wd, **_kw):
            def loader(sid, subpath=None):
                return list(jp.parse_jsonl_file(path)) if subpath is None else []
            return jp.parse_session_history_from_store(sdk, wd, loader, use_cache=False)

        import app.api.sessions_helpers as sh
        monkeypatch.setattr(sh, "parse_session_history_via_store", via_store)
        return path

    async def test_backfill_from_real_transcript_is_idempotent(self, seeded):
        await hb.ensure_backfilled(APP_SID)
        rows = se.get_events(APP_SID)
        assert [r["event_type"] for r in rows] == [
            "message.user", "message.assistant", "message.assistant",
            "tool.use", "tool.result", "message.assistant", "system.note",
            "message.user", "tool.use", "tool.result", "tool.use", "tool.result",
        ]
        assert [r["seq"] for r in rows] == list(range(1, 13))
        assert all(r["turn_id"] == "backfill" for r in rows)
        assert rows[0]["payload"]["uuid"] == "u1"
        assert rows[2]["payload"]["kind"] == "text"
        assert rows[1]["payload"]["kind"] == "thinking"
        assert rows[4]["payload"]["content_preview"] == "a.py\nb.py"
        assert rows[11]["payload"]["is_error"] is True
        # grow the transcript: a second read must NOT re-backfill
        with seeded.open("a") as f:
            f.write(json.dumps(user("u9", "later", i=50)) + "\n")
        await hb.ensure_backfilled(APP_SID)
        assert len(se.get_events(APP_SID)) == 12

    async def test_max_seq_failure_is_a_noop(self, seeded, monkeypatch):
        def boom(_sid):
            raise RuntimeError("db down")
        with monkeypatch.context() as m:
            m.setattr(se, "get_max_seq", boom)
            await hb.ensure_backfilled(APP_SID)
        assert se.get_events(APP_SID) == []

    async def test_unconvertible_history_is_memoed_as_empty(self, mock_db, monkeypatch):
        loads = []
        monkeypatch.setattr(hb, "_load_legacy_history",
                            lambda sid: loads.append(sid) or [{"role": "x", "type": "rate_limit"}])
        await hb.ensure_backfilled("sess-noise")
        await hb.ensure_backfilled("sess-noise")
        assert loads == ["sess-noise"]
        assert se.get_events("sess-noise") == []

    async def test_append_failure_does_not_raise(self, mock_db, monkeypatch):
        monkeypatch.setattr(hb, "_load_legacy_history",
                            lambda sid: [{"role": "user", "type": None, "content": "hi"},
                                         {"role": "assistant", "type": "text", "content": "yo"}])

        def boom(sid, events):
            raise RuntimeError("write failed")
        monkeypatch.setattr(se, "append_backfill_events", boom)
        await hb.ensure_backfilled("sess-fail")
        assert se.get_events("sess-fail") == []

    def test_missing_session_and_failing_loaders_yield_nothing(self, mock_db, monkeypatch):
        assert hb._load_legacy_history("no-such-session") == []
        sessions_db.create_session("sess-broken", "default")
        sessions_db.update_session("sess-broken", sdk_session_id=SID)
        import app.api.sessions_helpers as sh
        import app.core.db_message_loader as dml

        def boom(*a, **k):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(sh, "parse_session_history_via_store", boom)
        monkeypatch.setattr(dml, "load_messages_from_db", boom)
        assert hb._load_legacy_history("sess-broken") == []


# =============================================================================
# 9. Read/write error paths and on-disk damage
# =============================================================================

class TestErrorPaths:
    @pytest.mark.parametrize("variant", sorted(RAW_VARIANTS))
    def test_damage_matrix(self, projects, variant):
        path = write_raw(projects, RAW_VARIANTS[variant])
        assert [e.get("uuid") for e in jp.parse_jsonl_file(path)] == RAW_EXPECTED[variant]
        messages, _ = parse()
        assert [m["uuid"] for m in messages] == RAW_EXPECTED[variant]

    def test_non_utf8_record_is_reported_without_dropping_surrounding_messages(self, projects, caplog):
        """Strict decoding is record-local; corrupted text is not silently repaired."""
        path = write_raw(projects, NON_UTF8_FILE)
        messages, _ = parse()
        assert [(m["uuid"], m["content"]) for m in messages] == [
            ("v1", "first"), ("v2", "second"), ("v3", "third"),
        ]
        assert f"Skipping undecodable line 3 in {path}" in caplog.text

    def test_non_object_lines_are_skipped_not_fatal(self, projects):
        """Regression: ``null`` / ``42`` lines reached the transform, which
        crashed on ``entry.get`` and failed the whole history load."""
        path = write_raw(projects, NON_OBJECT_FILE)
        assert [e["uuid"] for e in jp.parse_jsonl_file(path)] == ["v1", "v2", "v3"]
        messages, _ = parse()
        assert [m["uuid"] for m in messages] == ["v1", "v2", "v3"]

    def test_missing_file_returns_empty_and_is_memoised(self, projects, monkeypatch):
        assert jp.parse_session_history(SID, WD) == ([], {})
        probes = []
        real_exists = Path.exists
        with monkeypatch.context() as m:
            m.setattr(Path, "exists", lambda self: probes.append(self) or real_exists(self))
            assert jp.get_session_jsonl_path(SID, WD) is None
        assert probes == []  # negative memo: no directory walk
        write_raw(projects, RAW_VARIANTS["clean"])
        jp.invalidate_parse_cache(SID)  # what a transcript change triggers
        assert len(jp.parse_session_history(SID, WD)[0]) == 3

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
    def test_unreadable_file_degrades_to_empty(self, projects):
        path = write_raw(projects, RAW_VARIANTS["clean"])
        path.chmod(0)
        try:
            assert list(jp.parse_jsonl_file(path)) == []
            assert parse() == ([], {})
            assert jp._get_session_title_and_preview(path) == (None, None, 0)
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_write_system_message_after_a_torn_line(self, projects):
        """Regression: the entry was appended onto the torn line, producing
        one invalid line — the system message was lost on every read."""
        write_raw(projects, RAW_VARIANTS["torn_last_line"])
        jp.write_system_message(SID, WD, "worktree_context", "Now in worktree", {"branch": "b1"})
        messages, _ = parse()
        assert [m.get("uuid", "")[:4] for m in messages] == ["v1", "v2", "sys-"]
        assert messages[-1]["subtype"] == "worktree_context"
        assert messages[-1]["systemData"] == {"branch": "b1"}

    def test_write_system_message_on_clean_file_adds_no_blank_line(self, projects):
        path = write_raw(projects, RAW_VARIANTS["clean"])
        jp.write_system_message(SID, WD, "plugin_warning", "careful")
        lines = path.read_bytes().split(b"\n")
        assert len(lines) == 5 and lines[-1] == b"" and all(lines[:4])

    def test_write_system_message_without_transcript_is_a_noop(self, projects):
        jp.write_system_message("0f0e0d0c-0b0a-4000-8000-00000000abcd", WD, "x", "y")
        assert list(projects.iterdir()) == []

    def test_hooks_sidecar_merged_by_timestamp_and_never_listed(self, projects):
        write_entries(projects, fx_cli2_turn())
        (projects / f"{SID}.hooks.jsonl").write_text(json.dumps({
            "type": "plugin_warning", "plugin": "lint", "message": "slow hook",
            "severity": "warning", "timestamp": _ts(3),
        }) + "\n")
        messages, _ = parse()
        # merged strictly-before the first message stamped AFTER it
        assert [m.get("subtype") or m["type"] for m in messages] == [
            None, "thinking", "text", "plugin_warning", "tool_use", "text", "stop_hook_summary",
        ]
        warn = messages[3]
        assert warn["systemData"] == {"plugin": "lint", "message": "slow hook", "severity": "warning"}
        # Regression: the sidecar was listed as a phantom "Session …" entry.
        listed = jp.list_chat_history_sessions(WD)
        assert [s["sdk_session_id"] for s in listed] == [SID]
        assert listed[0]["title"] == "List the files"

    def test_reading_a_session_found_elsewhere_creates_no_ghost_dir(self, projects):
        """Regression: the hooks-sidecar lookup mkdir'd the COMPUTED project
        dir on every parse, leaving empty "ghost" project dirs behind."""
        write_entries(projects, fx_cli2_turn())
        ghost_wd = "/some/other checkout"
        messages, _ = jp.parse_session_history(SID, ghost_wd)
        assert len(messages) == 6
        root = projects.parent
        assert sorted(p.name for p in root.iterdir()) == [projects.name]
