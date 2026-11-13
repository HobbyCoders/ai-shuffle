"""Context-efficiency phase 1a: long tool output is cut, spilled and countable.

Pinned here:

- the caps (cell stdout 8k, Bash/general 12k, WebFetch 16k, session search,
  view and recall 8k, HistoryExpand 8k by default);
- a cut over the cap writes the full output to the session's harness scratch
  folder and the inline text names it with the cut line range;
- a failed write falls back to plain head + tail truncation;
- command output keeps the larger share for its tail, so a trailing error
  survives, and a big single print in a cell keeps its tail too;
- a later Read of a spill file counts per session for the tool that wrote it,
  and a Grep or Glob aimed at the spill folder counts once as unattributed;
  only the call's explicit path is looked at, resolved, and only this
  session's folder counts;
- the file never holds less than the marker claims: Bash spills the cleaned
  text it captured, and a capture that dropped its middle says so; a timeout
  keeps the output up to it; a cell over the 2 MiB hand-off says its middle
  was not kept, and search listings spill too.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fluid.tools import spill
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry


def _ctx(tmp_path: Path, session: str = "sess-1") -> ToolCtx:
    return ToolCtx(
        cwd=str(tmp_path), session_id=session,
        extra={"harness_state_root": tmp_path / "state"},
    )


def _spill_path(text: str) -> Path:
    match = re.search(r"saved to (.+?\.txt)", text)
    assert match, text
    return Path(match.group(1))


# ── caps ────────────────────────────────────────────────────────────────────


def test_caps() -> None:
    from fluid.codemode import truncate as cm_truncate
    from fluid.codemode.subprocess_kernel import KernelConfig
    from fluid.context import recall
    from fluid.tools.builtins import _util, session_search, session_view, web

    assert cm_truncate.DEFAULT_STDOUT_CAP == 8_000
    assert KernelConfig().stdout_cap == 8_000
    assert _util._TRUNCATE_LIMIT == 12_000
    assert web.MAX_FETCH_CHARS == web.DEFAULT_FETCH_CHARS == 16_000
    assert session_search._OUTPUT_CAP == 8_000
    assert session_view._OUTPUT_CAP == 8_000
    assert recall._SEARCH_OUTPUT_CAP == 8_000
    assert recall.DEFAULT_EXPAND_CHARS == 8_000
    assert recall.MAX_EXPAND_CHARS == 16_000


def test_descriptions_state_the_new_limits() -> None:
    from fluid.codemode.session import _run_python_desc
    from fluid.tools.builtins import bash, history_expand, web

    assert "12,000 characters" in bash.describe()
    assert "30KB" not in bash.describe()
    assert "16,000" in json.dumps(web.FETCH_SPEC.input_schema)
    assert "max 16,000" in json.dumps(history_expand.SPEC.input_schema)
    desc = _run_python_desc()
    assert "8,000 characters" in desc
    assert "Filter and summarize inside the cell" in desc


# ── spill + marker ──────────────────────────────────────────────────────────


def test_under_the_cap_is_untouched_and_writes_nothing(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    assert spill.cut("short", 100, ctx=ctx, tool="Bash") == "short"
    assert not (tmp_path / "state").exists()


def test_over_the_cap_spills_the_full_text_and_names_it(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    text = "".join(f"line {i:05d}\n" for i in range(5_000))
    out = spill.cut(text, 2_000, ctx=ctx, tool="Bash", head_share=1 / 3)

    assert len(out) < 2_400
    assert out.startswith("line 00000\n")
    assert out.rstrip("\n").endswith("line 04999")
    path = _spill_path(out)
    # The file is the session's harness scratch folder, the one the
    # "# Harness scratch" reminder names, under tool-output/.
    from fluid.state import scratch_dir

    assert path.parent == scratch_dir(ctx) / "tool-output"
    assert path.name.startswith("Bash-")
    assert path.read_text(encoding="utf-8") == text
    # The marker gives the cut line range in 1-based file lines, so a Read
    # with that offset lands on the first missing line.
    m = re.search(r"cut lines ([\d,]+)-([\d,]+) of ([\d,]+)", out)
    assert m, out
    first, last, total = (int(g.replace(",", "")) for g in m.groups())
    assert total == 5_000
    lines = text.splitlines()
    shown = out.splitlines()
    assert shown[0] == lines[0]
    assert lines[first - 1] not in out and lines[first - 2] in out
    assert lines[last - 1] not in out and lines[last] in out
    assert "Read it with offset/limit or Grep it" in out
    assert spill.session_stats(ctx)["spilled"] == {"Bash": 1}


def test_same_output_maps_to_the_same_file(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    text = "x" * 50_000
    first = spill.cut(text, 1_000, ctx=ctx, tool="Grep")
    second = spill.cut(text, 1_000, ctx=ctx, tool="Grep")
    assert first == second
    directory = spill.spill_dir(ctx)
    assert directory is not None
    assert len(list(directory.iterdir())) == 1


def test_write_failure_falls_back_to_plain_head_and_tail(tmp_path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    monkeypatch.setattr(spill, "spill_dir", lambda _ctx: blocker / "tool-output")
    text = "a" * 3_000 + "TRAILING ERROR"
    out = spill.cut(text, 1_000, ctx=ctx, tool="Bash")
    assert "saved to" not in out
    assert "chars truncated" in out
    assert out.endswith("TRAILING ERROR")


def test_without_a_ctx_the_old_truncation_is_kept(tmp_path) -> None:
    from fluid.tools.builtins._util import truncate

    out = truncate("b" * 30_000)
    assert "chars truncated" in out and len(out) < 12_100


def test_command_output_keeps_the_larger_share_for_its_tail(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    noise = "".join(f"compiling unit {i}\n" for i in range(4_000))
    trace = "Traceback (most recent call last):\n" + "  frame\n" * 200 + "ValueError: boom\n"
    out = spill.cut(noise + trace, 12_000, ctx=ctx, tool="Bash", head_share=1 / 3)
    head, _, tail = out.partition("... [cut ")
    assert len(tail) > len(head)
    assert "Traceback (most recent call last):" in tail
    assert tail.rstrip().endswith("ValueError: boom")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
async def test_bash_spills_long_output_and_keeps_the_trailing_error(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = _ctx(tmp_path)
    command = "for i in $(seq 1 3000); do echo \"line $i of noise\"; done; echo FATAL-AT-END"
    out = await registry.dispatch("Bash", {"command": command}, ctx)
    assert "FATAL-AT-END" in out.text
    path = _spill_path(out.text)
    full = path.read_text(encoding="utf-8")
    assert "line 1500 of noise" in full and "line 1500 of noise" not in out.text
    assert full.rstrip().endswith("FATAL-AT-END")


# ── code mode ───────────────────────────────────────────────────────────────


def test_a_big_single_print_keeps_its_tail() -> None:
    from fluid.codemode.truncate import fair_share_stdout

    body = "".join(f"row {i}\n" for i in range(5_000)) + "SUMMARY: 3 failures\n"
    out = fair_share_stdout([body], 8_000)
    assert out.startswith("row 0\n")
    assert "SUMMARY: 3 failures" in out
    assert len(out) < 9_000


def test_cell_stdout_spill_pointer(tmp_path, monkeypatch) -> None:
    from fluid.codemode.session import _spill_cell_stdout

    ctx = _ctx(tmp_path)
    assert _spill_cell_stdout("", ctx) == ""
    note = _spill_cell_stdout("y" * 20_000, ctx)
    path = _spill_path(note)
    assert path.name.startswith("run_python-")
    assert path.read_text(encoding="utf-8") == "y" * 20_000
    monkeypatch.setattr(spill, "write_spill", lambda *a, **k: None)
    assert _spill_cell_stdout("y" * 20_000, ctx) == ""


async def test_kernel_hands_back_the_full_stdout_of_a_cut_cell() -> None:
    from fluid.codemode.kernel import HostReply
    from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

    async def _host(name: str, kwargs: dict) -> HostReply:
        return HostReply(result="")

    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        small = await kernel.exec("print('hi')", timeout=20, on_host_call=_host)
        assert small.stdout_full == ""
        big = await kernel.exec(
            "for i in range(3000):\n    print(f'row {i}')\nprint('LAST LINE')",
            timeout=20, on_host_call=_host,
        )
        assert len(big.stdout) < 10_000
        assert "row 1500\n" in big.stdout_full
        assert big.stdout_full.rstrip().endswith("LAST LINE")
        again = await kernel.exec("print('ok')", timeout=20, on_host_call=_host)
        assert again.stdout_full == ""
    finally:
        await kernel.stop()


# ── read-back telemetry ─────────────────────────────────────────────────────


async def test_reading_a_spill_file_back_is_counted(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = _ctx(tmp_path)
    out = spill.cut("z\n" * 20_000, 1_000, ctx=ctx, tool="Bash")
    path = _spill_path(out)

    read = await registry.dispatch("Read", {"file_path": str(path), "limit": 5}, ctx)
    assert not read.is_error
    # A search aimed at the folder counts once, for nobody in particular.
    await registry.dispatch("Grep", {"pattern": "z", "path": str(path.parent), "limit": 3}, ctx)
    await registry.dispatch("Glob", {"pattern": "*.txt", "path": str(path.parent)}, ctx)
    # An unrelated read is not counted, nor a command that names the file.
    other = tmp_path / "other.txt"
    other.write_text("hello")
    await registry.dispatch("Read", {"file_path": str(other)}, ctx)
    spill.note_access("Bash", {"command": f"cat {path}"}, ctx)

    stats = spill.session_stats(ctx)
    assert stats["read_back"] == {"Bash": 1, "unattributed": 2}
    assert stats["read_back_by"] == {"Read": 1, "Grep": 1, "Glob": 1}
    assert stats["spilled"] == {"Bash": 1}
    # Per session: another session's ledger is untouched.
    assert spill.session_stats(_ctx(tmp_path, "sess-2")) == {}


def test_a_relative_path_resolves_against_the_session_cwd(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    path = spill.write_spill("data", ctx=ctx, tool="run_python")
    assert path is not None
    relative = Path("..") / tmp_path.name / path.relative_to(tmp_path)
    spill.note_access("Read", {"file_path": str(relative)}, ctx)
    assert spill.session_stats(ctx)["read_back"] == {"run_python": 1}


def test_paths_outside_this_sessions_folder_are_not_counted(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    mine = spill.write_spill("mine", ctx=ctx, tool="Bash")
    theirs = spill.write_spill("theirs", ctx=_ctx(tmp_path, "sess-2"), tool="Bash")
    assert mine is not None and theirs is not None
    # Another session's spill file, by absolute path (same name shape).
    spill.note_access("Read", {"file_path": str(theirs)}, ctx)
    spill.note_access("Grep", {"pattern": "x", "path": str(theirs.parent)}, ctx)
    # A ``..`` that climbs out of the folder after naming it.
    spill.note_access("Read", {"file_path": str(mine.parent / ".." / "notes.txt")}, ctx)
    # A link inside the folder that points elsewhere resolves elsewhere.
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = mine.parent / "Bash-0123456789ab.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None
    if link is not None:
        spill.note_access("Read", {"file_path": str(link)}, ctx)
    assert "read_back" not in spill.session_stats(ctx)
    assert "read_back" not in spill.session_stats(_ctx(tmp_path, "sess-2"))


# ── the file holds what the marker says ─────────────────────────────────────


def test_a_capture_that_dropped_its_middle_says_so(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import bash
    from fluid.tools.builtins._util import strip_ansi

    monkeypatch.setattr(bash, "MAX_CAPTURE_BYTES", 40_000)
    ctx = _ctx(tmp_path)
    capture = bash._Capture()
    raw = "".join(f"\x1b[32mrow {i:06d}\x1b[0m\n" for i in range(10_000))
    for i in range(0, len(raw), 7_000):
        capture.add(raw[i:i + 7_000].encode())
    assert capture.dropped > 0
    out = bash._render(capture, ctx)
    cleaned = strip_ansi(capture.value().decode())
    # The file is exactly the cleaned text the inline head and tail came from.
    assert _spill_path(out).read_text(encoding="utf-8") == cleaned
    assert "\x1b" not in out
    assert "full output" not in out
    assert f"captured output saved to {_spill_path(out)}" in out
    assert f"the middle ~{capture.dropped:,} bytes were not captured" in out
    assert out.rstrip().endswith("row 009999")
    # Same output, same marker: the result text is stable for the cache.
    assert bash._render(capture, ctx) == out


def test_cell_stdout_over_the_handoff_limit_says_its_middle_was_not_kept(tmp_path) -> None:
    from fluid.codemode.session import _spill_cell_stdout
    from fluid.codemode.truncate import FULL_STDOUT_MAX, bounded_full_stdout

    ctx = _ctx(tmp_path)
    total = FULL_STDOUT_MAX + 500_000
    sent = bounded_full_stdout("q" * total)
    note = _spill_cell_stdout(sent, ctx, total)
    assert "all " not in note
    assert f"the middle {total - FULL_STDOUT_MAX:,} chars were not kept" in note
    assert _spill_path(note).read_text(encoding="utf-8") == sent


def test_session_search_listing_over_the_cap_spills(tmp_path) -> None:
    from fluid.tools.builtins import session_search

    ctx = _ctx(tmp_path)
    listing = "".join(f"hit {i:04d} some matched text here\n" for i in range(1_000))
    out = session_search._capped(listing, ctx)
    assert len(out) < session_search._OUTPUT_CAP + 400
    assert out.startswith("hit 0000")
    assert _spill_path(out).read_text(encoding="utf-8") == listing


def test_history_search_listing_over_the_cap_spills(tmp_path) -> None:
    from fluid.context import recall

    ctx = _ctx(tmp_path)
    hits = [
        recall.Hit(node_id=f"node_{i:012x}", role="user", snippet=f"needle number {i} " * 6, matches=1)
        for i in range(400)
    ]
    out = recall.render_hits(hits, "needle", ctx=ctx)
    assert len(out) < recall._SEARCH_OUTPUT_CAP + 400
    assert "needle number 399" in _spill_path(out).read_text(encoding="utf-8")
    assert spill.session_stats(ctx)["spilled"] == {"HistorySearch": 1}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
async def test_a_timed_out_command_keeps_its_output_up_to_the_timeout(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = _ctx(tmp_path)
    command = (
        "for i in $(seq 1 3000); do echo \"line $i of noise\"; done; "
        "echo FATAL-BEFORE-HANG; sleep 30"
    )
    out = await registry.dispatch("Bash", {"command": command, "timeout": 2}, ctx)
    assert out.is_error
    assert "timed out after 2s" in out.text
    assert "output up to the timeout saved to" in out.text
    assert "FATAL-BEFORE-HANG" in out.text
    full = _spill_path(out.text).read_text(encoding="utf-8")
    assert "line 1500 of noise" in full
    assert full.rstrip().endswith("FATAL-BEFORE-HANG")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
async def test_a_cancelled_command_writes_no_file(tmp_path) -> None:
    import asyncio

    from fluid.tools.builtins import bash

    ctx = _ctx(tmp_path)
    command = "for i in $(seq 1 3000); do echo \"line $i of noise\"; done; sleep 30"
    task = asyncio.ensure_future(bash.handler({"command": command}, ctx))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = spill.spill_dir(ctx)
    assert directory is not None
    assert not directory.exists() or not any(directory.iterdir())
