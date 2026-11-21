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
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode, new_id

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
        self.checkpoint: dict = {}
        out = compact_messages(posted, self.stored, context_window=200_000, trigger=trigger,
                               report=report, checkpoint=self.checkpoint)
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


def test_a_cut_input_is_not_cut_again():
    value = {"command": "c" * 9_000_000, "content": "d" * 301}
    short = short_input(value, new_id("node"))
    assert short_input(short, new_id("node")) is short


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


def script(tid: str) -> dict:
    return {"command": f"SCRIPT-{tid} " + "echo step; " * 300, "description": "Run the steps"}


def test_only_the_input_changes_in_a_cut_row():
    """The row is Claude Code's own with a shorter input: its result row, and
    every row around it, is handed back by its handle."""
    chat = Chat()
    ids = chat.write(30)
    posted = {row["handle"]: row for row in chat.cli}
    out, _ = chat.compact()
    cut = [row for row in out if CUT in json.dumps(row["toolUses"])]
    assert {use["tool_use_id"] for row in cut for use in row["toolUses"]} == set(ids[:-KEEP_INPUT_CALLS])
    assert all("handle" not in row for row in cut)
    kept = [row for row in out if "handle" in row]
    assert all(posted[row["handle"]] == row for row in kept)
    assert len(kept) == 1 + 30 + KEEP_INPUT_CALLS  # the request, every result, the newest calls


def text_and_calls(chat: Chat, said: str, together: bool) -> dict:
    """A streamed message stored as a text node and call nodes of one uuid:
    Claude Code's row is the text with the first call (``together``: with
    both calls). One result is long enough for a later compaction to trim."""
    row = chat.add(Message("assistant", [Text(said)]))
    for n, tid in enumerate(("g0", "g1")):
        message = Message("assistant", [ToolUseBlock(tid, "Bash", script(tid))])
        if n == 0 or together:
            message.provider_meta["claude_uuid"] = row["handle"]
            chat.stored.append(SessionNode(f"node_{len(chat.stored):012x}", chat.stored[-1].node_id,
                                           message))
            row["toolUses"] += project(message)["toolUses"]
        else:
            chat.add(message)
    chat.add(Message("user", [ToolResultBlock("g0", [Text("done")])]))
    chat.add(Message("user", [ToolResultBlock("g1", [Text("RESULT " + "r" * 8_500)])]))
    return row


def test_a_tool_group_with_text_is_left_whole_so_the_text_is_never_dropped():
    """A cut row is matched by its tool ids the next time, and so is its
    group; text beside a call has no stored node there, and a rebuild of the
    group (to trim a result) would drop it. Such a group is not cut."""
    for together in (False, True):
        chat = Chat()
        chat.write(2)
        said = "NOTE-FOR-LATER: these two scripts build the importer in order."
        row = text_and_calls(chat, said, together)
        chat.write(12)
        for _ in range(3):
            out, _ = chat.compact()
            shown = inputs(out)
            assert shown["g0"] == script("g0") and shown["g1"] == script("g1")
            assert row in out  # Claude Code's own row, by its handle
            assert CUT in shown["t0"]["content"]  # the calls around the group are cut
            chat.write(20)


def test_cut_inputs_do_not_move_the_portable_checkpoint():
    """The checkpoint is rows another worker replays as text. It ends where
    the ladder's work ends; a shorter input later on must not carry it past a
    row it cannot reproduce (a screenshot in a result)."""
    chat = Chat()
    chat.call("Read", {"file_path": "src/big.py"}, result="x" * 60_000)  # trimmed by the ladder
    chat.add(Message("assistant", [ToolUseBlock("shot", "mcp__browser__screenshot", {})]))
    chat.add(Message("user", [ToolResultBlock(
        "shot", [Text("Screenshot of the page"), Image(media_type="image/png", data="AAAA")],
        is_error=True)]))
    screenshot = len(chat.stored) - 1
    chat.write(30)
    _, report = chat.compact()
    assert report["input_tokens_cut"] > 15_000
    through = next(i for i, node in enumerate(chat.stored)
                   if node.node_id == chat.checkpoint["through"])
    assert through < screenshot
    assert CUT not in json.dumps(chat.checkpoint["rows"])


def screenshot(chat: Chat) -> int:
    """A screenshot result Claude Code holds whole; its stored index."""
    chat.add(Message("assistant", [ToolUseBlock("shot", "mcp__browser__screenshot", {})]))
    chat.add(Message("user", [ToolResultBlock(
        "shot", [Text("Screenshot of the page"), Image(media_type="image/png", data="AAAA")])]))
    return len(chat.stored) - 1


def big_writes(chat: Chat, n: int = 23) -> None:
    """Writes whose inputs alone are over the checkpoint's size limit."""
    for _ in range(n):
        chat.call("Write", {"file_path": f"src/big_{chat.calls}.py",
                            "content": f"BODY-{chat.calls} " + "x" * 18_000}, result="File created")


def test_shorter_inputs_do_not_add_a_checkpoint_that_holds_a_screenshot_as_text():
    """A checkpoint's rows are text: it stands for a screenshot as its caption
    and a replay from it skips the stored node that has the image. With the
    inputs whole there was none here (auto: the compaction saved too little
    to be answered; manual: the checkpoint was over its size limit). The cut
    makes the answer worth giving and the checkpoint small enough; the answer
    is given, the checkpoint still is not written."""
    for trigger in ("auto", "manual"):
        chat = Chat()
        big_writes(chat)
        screenshot(chat)
        held = chat.cli[-1]
        chat.call("Read", {"file_path": "src/big.py"}, result="x" * 60_000)  # trimmed by the ladder
        chat.write(8)
        out, report = chat.compact(trigger)
        assert report["input_tokens_cut"] > 23 * 18_000 // 4
        assert held in out  # the live answer keeps Claude Code's row, image and all
        assert "rows" not in chat.checkpoint and "through" not in chat.checkpoint


def test_inputs_an_earlier_compaction_cut_do_not_add_that_checkpoint_either():
    """By the next compaction Claude Code holds the cut inputs as its own
    rows. The checkpoint is still judged with every input as stored."""
    chat = Chat()
    big_writes(chat)
    chat.write(8)
    chat.compact()
    assert all(CUT in json.dumps(row["toolUses"]) for row in chat.cli[1:47:2])
    screenshot(chat)
    chat.call("Read", {"file_path": "src/big.py"}, result="x" * 60_000)
    chat.write(8)
    _, report = chat.compact("manual")
    assert report["input_tokens_cut"] < 23 * 18_000 // 4  # the big inputs were cut before
    assert "rows" not in chat.checkpoint and "through" not in chat.checkpoint


def test_a_checkpoint_the_whole_inputs_also_gave_is_written_as_it_was():
    """Where the answer with whole inputs writes a checkpoint too, it is the
    one it was: through the last compacted row, with the rows only Claude
    Code holds (the instructions of a skill being followed)."""
    chat = Chat()
    chat.call("Read", {"file_path": "src/first.py"}, result="x" * 60_000)  # trimmed by the ladder
    screenshot(chat)
    chat.cli.append({"role": "user", "text": "SKILL-INSTRUCTIONS: follow these steps in order.",
                     "toolUses": [], "handle": "claude-only"})
    chat.call("Read", {"file_path": "src/second.py"}, result="y" * 60_000)  # trimmed too
    second = len(chat.stored) - 1
    chat.write(30)
    out, _ = chat.compact()
    assert any("SKILL-INSTRUCTIONS" in row["text"] for row in out)
    assert chat.checkpoint["through"] == chat.stored[second].node_id
    assert "SKILL-INSTRUCTIONS" in json.dumps(chat.checkpoint["rows"])


def test_the_newest_calls_are_counted_call_by_call_in_a_parallel_row():
    """Seven writes made together are one row. Two newer calls leave four of
    them among the newest: the three oldest are cut, the big one first, and
    the compaction saves enough to be used."""
    chat = Chat()
    ids = [f"w{n}" for n in range(7)]
    big = {"file_path": "src/w0.py", "content": "BIG " + "x" * 150_000}
    chat.add(Message("assistant", [
        ToolUseBlock(tid, "Write", big if tid == "w0" else {
            "file_path": f"src/{tid}.py", "content": f"BODY-{tid} " + "line of code\n" * 250})
        for tid in ids]))
    chat.add(Message("user", [ToolResultBlock(tid, [Text("File created")]) for tid in ids]))
    newer = [chat.call("Read", {"file_path": f"src/r{n}.py"}, result="x" * 9_000) for n in range(2)]
    out, report = chat.compact()
    shown = inputs(out)
    assert list(shown) == ids + newer
    assert all(CUT in shown[tid]["content"] for tid in ids[:3])
    assert all(CUT not in shown[tid]["content"] for tid in ids[3:])
    assert report["input_tokens_cut"] > 150_000 // 4


def test_a_group_with_an_image_result_is_left_as_claude_code_has_it():
    """A tool group with a block only Claude Code holds stays its own rows,
    also when each of its rows matches a stored node."""
    chat = Chat()
    chat.write(4)
    rows = [
        chat.add(Message("assistant", [ToolUseBlock("b0", "Bash", script("b0"))])),
        chat.add(Message("assistant", [ToolUseBlock("r0", "Read", {"file_path": "shot.png"})])),
        chat.add(Message("user", [ToolResultBlock("b0", [Text("done")])])),
        chat.add(Message("user", [ToolResultBlock("r0", [Image(media_type="image/png", data="AAAA")])])),
    ]
    chat.write(20)
    out, _ = chat.compact()
    assert inputs(out)["b0"] == script("b0")
    handles = {row.get("handle") for row in out}
    assert all(row["handle"] in handles for row in rows)
    assert CUT in inputs(out)["t0"]["content"]  # the calls around it are cut as usual
