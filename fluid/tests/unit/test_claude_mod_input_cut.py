"""A compaction of a Claude Code chat cuts the long inputs of old tool calls.

The request being worked on is never digested, so its calls stay as the model
made them. Results are trimmed, but the text inside an old call's input (a
script, a file's new content, an edit's strings) stayed whole: on a long
autonomous request that was most of what a compaction kept, and the chat
reached its next compaction sooner. A finished call older than the newest few
keeps its keys and short values; a long string is cut to its opening and names
the stored node that reads it back whole.
"""
from __future__ import annotations

import itertools
import json
import re

from fluid.engine.claude_mod_compaction import (
    INPUT_HEAD_CHARS,
    INPUT_STRING_CHARS,
    KEEP_INPUT_CALLS,
    compact_messages,
    project,
    short_input,
)
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode

from tests.unit.test_claude_mod_compaction_cycles import assert_mod_accepts, read_whole

CUT = "chars cut by compaction"


class Chat:
    """A stored path and the rows Claude Code holds for it: one request the
    model is still working on, a call at a time."""

    def __init__(self) -> None:
        self.stored: list[SessionNode] = []
        self.cli: list[dict] = []
        self._ids = itertools.count()
        self.calls = 0
        self.add(Message("user", [Text("Request: write the whole importer, do not stop.")]))

    def add(self, message: Message) -> dict:
        uuid = f"u{next(self._ids)}"
        message.provider_meta["claude_uuid"] = uuid
        node = SessionNode(f"node_{len(self.stored):012x}",
                           self.stored[-1].node_id if self.stored else None, message)
        self.stored.append(node)
        row = {**project(message), "handle": uuid}
        self.cli.append(row)
        return row

    def call(self, tool: str, input_: dict, *, result: str = "ok", error: bool = False) -> str:
        tid = f"t{self.calls}"
        self.calls += 1
        self.add(Message("assistant", [ToolUseBlock(tid, tool, input_)]))
        self.add(Message("user", [ToolResultBlock(tid, [Text(result)], is_error=error)]))
        return tid

    def write(self, n: int = 1, **kwargs) -> list[str]:
        return [self.call("Write", {"file_path": f"src/module_{self.calls}.py",
                                    "content": f"BODY-{self.calls} " + "line of code\n" * 250},
                          result="File created", **kwargs) for _ in range(n)]

    def compact(self, trigger: str = "auto") -> tuple[list[dict], dict]:
        posted = [dict(r) for r in self.cli]
        report: dict = {}
        out = compact_messages(posted, self.stored, context_window=200_000, trigger=trigger,
                               report=report)
        assert_mod_accepts(out, posted)
        # Claude Code keeps a handled row as it is and builds the rest anew.
        self.cli = [r if "handle" in r else {**r, "handle": f"built{next(self._ids)}"} for r in out]
        return out, report


def inputs(rows: list[dict]) -> dict[str, dict]:
    return {use["tool_use_id"]: use["input"] for row in rows for use in row["toolUses"]}


def test_short_input_cuts_long_strings_and_keeps_the_shape():
    value = {"file_path": "src/a.py", "content": "c" * 5_000, "limit": 3, "flag": True,
             "edits": [{"old": "o" * 301, "new": "short"}]}
    short = short_input(value, "node_7")
    assert list(short) == list(value) and short["file_path"] == "src/a.py"
    assert short["limit"] == 3 and short["flag"] is True
    assert short["content"].startswith("c" * INPUT_HEAD_CHARS + "…")
    assert f"+{5_000 - INPUT_HEAD_CHARS} {CUT}" in short["content"]
    assert 'HistoryExpand("node_7")' in short["content"]
    assert len(short["content"]) < INPUT_STRING_CHARS
    assert CUT in short["edits"][0]["old"] and short["edits"][0]["new"] == "short"
    assert value["content"] == "c" * 5_000  # the stored input is not edited


def test_short_input_returns_the_same_object_when_nothing_is_long():
    value = {"command": "c" * INPUT_STRING_CHARS, "items": ["a", {"b": 1}], "n": None}
    assert short_input(value, "node_7") is value


def test_old_calls_of_the_working_request_lose_their_long_inputs():
    chat = Chat()
    ids = chat.write(30)
    before = len(json.dumps(chat.cli))
    out, report = chat.compact()
    shown = inputs(out)
    assert set(shown) == set(ids)  # every call is still a call, with its result
    for tid in ids[:-KEEP_INPUT_CALLS]:
        content = shown[tid]["content"]
        assert content.startswith(f"BODY-{tid[1:]} line of code") and CUT in content
        assert len(content) < INPUT_STRING_CHARS
        assert shown[tid]["file_path"] == f"src/module_{tid[1:]}.py"
    for tid in ids[-KEEP_INPUT_CALLS:]:
        assert CUT not in shown[tid]["content"] and len(shown[tid]["content"]) > 3_000
    assert len(json.dumps(out)) < before * 0.4
    assert report["input_tokens_cut"] > 15_000
    assert any("Request: write the whole importer" in row["text"] for row in out)


def test_the_cut_names_the_node_that_reads_the_call_back_whole():
    chat = Chat()
    ids = chat.write(20)
    out, _ = chat.compact()
    content = inputs(out)[ids[3]]["content"]
    node_id = re.search(r'HistoryExpand\("([^"]+)"\)', content).group(1)
    whole = read_whole(chat.stored, node_id)
    assert "BODY-3 " + "line of code\\n" * 250 in whole


def test_a_failed_call_keeps_its_input():
    chat = Chat()
    chat.write(4)
    failed = chat.write(1, error=True)[0]
    chat.write(20)
    out, _ = chat.compact()
    shown = inputs(out)
    assert CUT not in shown[failed]["content"] and len(shown[failed]["content"]) > 3_000
    assert CUT in shown["t0"]["content"] and CUT in shown["t5"]["content"]


def test_a_plan_and_a_brief_keep_their_inputs():
    chat = Chat()
    plan = {"steps": [{"title": f"Step {n}: " + "detail " * 60} for n in range(8)]}
    brief = {"agent": "engineer", "prompt": "Implement the importer. " * 80}
    chat.call("Plan", plan)
    chat.call("Task", brief, result="started")
    chat.write(25)
    out, _ = chat.compact()
    shown = inputs(out)
    assert shown["t0"] == plan and shown["t1"] == brief
    assert CUT in shown["t2"]["content"]


def test_short_inputs_are_left_as_claude_code_has_them():
    chat = Chat()
    for n in range(30):
        chat.call("Read", {"file_path": f"src/file_{n}.py"}, result="x" * 6_000)
    out, report = chat.compact()
    assert report["input_tokens_cut"] == 0
    assert all(CUT not in json.dumps(use["input"]) for row in out for use in row["toolUses"])


def test_a_second_compaction_keeps_the_cut_and_claude_codes_own_rows():
    chat = Chat()
    first = chat.write(30)
    chat.compact()
    cut_rows = {use["tool_use_id"]: row["handle"] for row in chat.cli for use in row["toolUses"]
                if CUT in json.dumps(use["input"])}
    assert set(cut_rows) == set(first[:-KEEP_INPUT_CALLS])
    second = chat.write(30)
    out, report = chat.compact()
    shown = inputs(out)
    by_call = {use["tool_use_id"]: row for row in out for use in row["toolUses"]}
    for tid in first + second[:-KEEP_INPUT_CALLS]:
        assert CUT in shown[tid]["content"] and len(shown[tid]["content"]) < INPUT_STRING_CHARS
    # A row the first compaction cut is Claude Code's own row now: handed back
    # by its handle, not rebuilt, and not counted as cut again.
    for tid, handle in cut_rows.items():
        assert by_call[tid].get("handle") == handle
    newly = len(first[-KEEP_INPUT_CALLS:] + second[:-KEEP_INPUT_CALLS])
    whole = len(json.dumps({"file_path": "src/module_1.py", "content": "BODY-1 " + "line of code\n" * 250}))
    assert newly * (whole - 400) // 4 < report["input_tokens_cut"] < newly * whole // 4


def test_a_parallel_group_is_cut_and_stays_paired():
    chat = Chat()
    chat.write(4)
    # Two calls made together: the stream stored call, result, call, result
    # and Claude Code lists both calls before both results.
    rows = []
    for tid in ("p0", "p1"):
        rows.append(chat.add(Message("assistant", [ToolUseBlock(tid, "Bash", {
            "command": f"SCRIPT-{tid} " + "echo step; " * 300, "description": "Run the steps"})])))
        rows.append(chat.add(Message("user", [ToolResultBlock(tid, [Text("done")])])))
    del chat.cli[-4:]
    chat.cli += [rows[0], rows[2], rows[1], rows[3]]
    chat.write(20)
    out, _ = chat.compact()
    shown = inputs(out)
    for tid in ("p0", "p1"):
        assert shown[tid]["command"].startswith(f"SCRIPT-{tid} echo step;") and CUT in shown[tid]["command"]
        assert shown[tid]["description"] == "Run the steps"
    # And again, once Claude Code holds the cut group as its own rows.
    chat.write(20)
    out, _ = chat.compact()
    shown = inputs(out)
    assert all(CUT in shown[tid]["command"] and len(shown[tid]["command"]) < INPUT_STRING_CHARS
               for tid in ("p0", "p1"))
