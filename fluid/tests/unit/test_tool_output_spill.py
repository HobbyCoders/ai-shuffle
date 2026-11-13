"""Context-efficiency phase 1a: long tool output is cut, spilled and countable.

Pinned here:

- the caps (cell stdout 8k, Bash/general 12k, WebFetch 16k, session search,
  view and recall 8k, HistoryExpand 8k by default);
- a cut over the cap writes the full output to the session's harness scratch
  folder and the inline text names it with the cut line range;
- a failed write falls back to plain head + tail truncation;
- command output keeps the larger share for its tail, so a trailing error
  survives, and a big single print in a cell keeps its tail too;
- a later Read/Grep/Glob/Bash of a spill file is counted per session and per
  producing tool, from the files the call touched or returned (each producer
  once per call); a folder search that returns none counts as unattributed;
- the file never holds less than the marker claims: Bash streams the whole
  output to disk (to a byte ceiling the marker names), never splitting an
  escape sequence or a character; a timeout keeps the output up to it and an
  exception keeps the file, only a cancellation deletes it; a cell over the
  2 MiB hand-off names the range it lost, and search listings spill too.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry
from fluid.tools import spill


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
    # A Grep that returns the file's lines counts for the tool that wrote it.
    await registry.dispatch("Grep", {"pattern": "z", "path": str(path.parent), "limit": 3}, ctx)
    # A search of the folder that returns no file counts, but for nobody.
    await registry.dispatch("Grep", {"pattern": "no-such-text", "path": str(path.parent)}, ctx)
    # An unrelated read is not counted.
    other = tmp_path / "other.txt"
    other.write_text("hello")
    await registry.dispatch("Read", {"file_path": str(other)}, ctx)

    stats = spill.session_stats(ctx)
    assert stats["read_back"] == {"Bash": 2, "unattributed": 1}
    assert stats["read_back_by"] == {"Read": 1, "Grep": 2}
    assert stats["spilled"] == {"Bash": 1}
    # Per session: another session's ledger is untouched.
    assert spill.session_stats(_ctx(tmp_path, "sess-2")) == {}


def test_bash_naming_a_spill_file_counts_with_any_slash_or_case(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    path = spill.write_spill("data", ctx=ctx, tool="run_python")
    assert path is not None
    windowsy = str(path.parent).replace("/", "\\").upper() + "\\" + path.name
    spill.note_access("Bash", {"command": f"sed -n 1,40p '{windowsy}'"}, "data", ctx)
    assert spill.session_stats(ctx)["read_back"] == {"run_python": 1}


def _three_spills(ctx: ToolCtx) -> dict[str, Path]:
    paths = {}
    for tool in ("Bash", "run_python", "Grep"):
        path = spill.write_spill(f"{tool} output\n" * 9, ctx=ctx, tool=tool)
        assert path is not None
        paths[tool] = path
    return paths


def test_a_search_counts_each_producer_it_returned_once(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    paths = _three_spills(ctx)
    # Grep repeats a file on every matching line; still one count per producer.
    listing = "".join(f"{paths['Bash']}:{n}:Bash output\n" for n in range(1, 10))
    listing += f"{paths['run_python']}:1:run_python output\n"
    spill.note_access("Grep", {"pattern": "output", "path": str(paths["Bash"].parent)}, listing, ctx)
    stats = spill.session_stats(ctx)
    assert stats["read_back"] == {"Bash": 1, "run_python": 1}
    assert stats["read_back_by"] == {"Grep": 1}


async def test_a_glob_of_the_folder_counts_the_files_it_listed(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = _ctx(tmp_path)
    paths = _three_spills(ctx)
    # Glob lists names relative to the folder; the query is never interpreted.
    out = await registry.dispatch("Glob", {"pattern": "run_python-*", "path": str(paths["Bash"].parent)}, ctx)
    assert paths["run_python"].name in out.text
    assert spill.session_stats(ctx)["read_back"] == {"run_python": 1}


def test_spill_like_names_elsewhere_are_not_counted(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    paths = _three_spills(ctx)
    real = paths["Bash"].name
    noise = "\n".join([
        "wrote Bash-0123456789ab.txt",  # spill-shaped, but no such file here
        f"cache/{real}",  # a real name, but not in or aimed at the folder
        f"old-{real}",  # the tail of a longer name
        "/tmp/other/tool-output/Bash-0123456789ab.txt",  # another folder's file
    ])
    spill.note_access("Bash", {"command": "make all"}, noise, ctx)
    spill.note_access("Grep", {"pattern": "x", "path": str(tmp_path / "src")}, noise, ctx)
    assert "read_back" not in spill.session_stats(ctx)


def test_a_call_naming_the_file_it_just_spilled_is_not_a_read(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    out = spill.cut("q\n" * 20_000, 1_000, ctx=ctx, tool="Bash")
    spill.note_access("Bash", {"command": "seq 1 20000"}, out, ctx)
    assert "read_back" not in spill.session_stats(ctx)


# ── the file holds what the marker says ─────────────────────────────────────

_GREEN, _RESET = "\x1b[32m", "\x1b[0m"


def _feed(stream: spill.SpillStream, text: str, step: int = 7_000) -> None:
    data = text.encode()
    for i in range(0, len(data), step):
        stream.feed(data[i:i + step])


def test_a_streamed_spill_holds_the_middle_the_memory_copy_dropped(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    text = "".join(f"{_GREEN}row {i:06d}{_RESET}\n" for i in range(40_000))

    def strip(t: str) -> str:
        return t.replace(_GREEN, "").replace(_RESET, "")

    stream = spill.SpillStream(ctx, "Bash", open_after=1_000, transform=strip)
    _feed(stream, text)
    clean = strip(text)
    # What the caller kept in memory: head and tail only, the middle gone.
    kept = clean[:5_000] + clean[-5_000:]
    out = spill.cut_streamed(kept, 2_000, stream)
    path = _spill_path(out)
    assert path.read_text(encoding="utf-8") == clean
    assert "full output saved" in out
    assert f"of {40_000:,}" in out
    assert spill.session_stats(ctx)["spilled"] == {"Bash": 1}
    assert not list(path.parent.glob("*.part"))


def test_a_streamed_spill_over_its_ceiling_names_what_was_not_kept(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    text = "".join(f"row {i:06d}\n" for i in range(10_000))  # 110,000 chars
    stream = spill.SpillStream(ctx, "Bash", open_after=100, ceiling=20_000)
    _feed(stream, text)
    out = spill.cut_streamed(text[:3_000] + text[-3_000:], 1_200, stream, head_share=1 / 3)
    path = _spill_path(out)
    assert path.read_text(encoding="utf-8") == text[:20_000]
    assert "— full output saved" not in out
    assert "the first 20,000 chars of the full output saved" in out
    assert "20,000-byte cap" in out
    m = re.search(r"chars ([\d,]+)-([\d,]+) of ([\d,]+) were not kept", out)
    assert m, out
    lost_from, lost_to, total = (int(g.replace(",", "")) for g in m.groups())
    assert (lost_from, total) == (20_001, len(text))
    # Everything after the lost range is in the inline tail.
    assert out.endswith(text[lost_to:])


def test_short_streamed_output_writes_nothing(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    stream = spill.SpillStream(ctx, "Bash", open_after=12_000)
    _feed(stream, "ok\n" * 100)
    assert spill.cut_streamed("ok\n" * 100, 12_000, stream) == "ok\n" * 100
    assert spill.session_stats(ctx) == {}
    directory = spill.spill_dir(ctx)
    assert directory is not None
    assert not directory.exists() or not any(directory.iterdir())


def test_a_streamed_spill_that_cannot_write_falls_back(tmp_path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    monkeypatch.setattr(spill, "spill_dir", lambda _ctx: blocker / "tool-output")
    stream = spill.SpillStream(ctx, "Bash", open_after=10)
    text = "a" * 3_000 + "TRAILING ERROR"
    _feed(stream, text)
    out = spill.cut_streamed(text, 1_000, stream)
    assert "saved to" not in out and "chars truncated" in out
    assert out.endswith("TRAILING ERROR")


def test_cell_stdout_over_the_handoff_limit_names_the_lost_range(tmp_path) -> None:
    from fluid.codemode.session import _spill_cell_stdout
    from fluid.codemode.truncate import FULL_STDOUT_MAX, bounded_full_stdout

    ctx = _ctx(tmp_path)
    total = FULL_STDOUT_MAX + 500_000
    sent = bounded_full_stdout("q" * total)
    note = _spill_cell_stdout(sent, ctx, total)
    half = FULL_STDOUT_MAX // 2
    assert "all " not in note
    assert f"chars {half + 1:,}-{total - half:,} were not kept" in note
    assert f"{total - half + 1:,}-{total:,}" in note
    _spill_path(note)


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


def test_a_forced_flush_never_splits_an_escape_sequence(tmp_path) -> None:
    from fluid.tools.builtins._util import strip_ansi

    ctx = _ctx(tmp_path)
    # One unterminated line long enough to force a flush, with an escape
    # sequence arriving across the boundary.
    first = ("a" * (spill._FLUSH_CHARS - 1) + "\x1b").encode()
    second = "[31mFATAL\x1b[0m\n".encode()
    raw = (first + second).decode()
    stream = spill.SpillStream(ctx, "Bash", transform=strip_ansi)
    stream.feed(first)
    stream.feed(second)
    out = spill.cut_streamed(strip_ansi(raw), 1_000, stream)
    on_disk = _spill_path(out).read_text(encoding="utf-8")
    assert on_disk == strip_ansi(raw)
    assert "\x1b" not in on_disk
    assert stream.chars == len(strip_ansi(raw))


def test_a_multibyte_character_split_across_reads_stays_whole(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    data = ("é" * 9_000 + "\n").encode()
    stream = spill.SpillStream(ctx, "Bash")
    stream.feed(data[:7])  # ends mid-character
    stream.feed(data[7:])
    out = spill.cut_streamed(data.decode(), 1_000, stream)
    assert _spill_path(out).read_text(encoding="utf-8") == data.decode()


def test_the_disk_ceiling_counts_bytes_and_stops_at_a_character(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    text = "\U0001F600" * 3_000 + "\n"  # four bytes each
    stream = spill.SpillStream(ctx, "Bash", ceiling=1_001)
    _feed(stream, text)
    out = spill.cut_streamed(text[:300] + text[-300:], 400, stream)
    path = _spill_path(out)
    assert path.stat().st_size == 1_000  # 250 whole characters, never 1,001
    assert path.read_text(encoding="utf-8") == "\U0001F600" * 250
    assert "the first 250 chars of the full output saved" in out
    assert "1,001-byte cap" in out
    assert "chars 251-" in out


def test_leaving_the_stream_by_an_exception_keeps_the_file(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError), spill.SpillStream(ctx, "Bash", open_after=100) as stream:
        _feed(stream, "row\n" * 1_000)
        raise ValueError("tool failed")
    assert stream.path is not None
    assert stream.path.read_text(encoding="utf-8") == "row\n" * 1_000
    assert spill.session_stats(ctx)["spilled"] == {"Bash": 1}


def test_leaving_the_stream_by_cancellation_deletes_the_file(tmp_path) -> None:
    import asyncio

    ctx = _ctx(tmp_path)
    with pytest.raises(asyncio.CancelledError), spill.SpillStream(ctx, "Bash", open_after=100) as stream:
        _feed(stream, "row\n" * 1_000)
        raise asyncio.CancelledError
    directory = spill.spill_dir(ctx)
    assert directory is not None
    assert not any(directory.iterdir())
    assert spill.session_stats(ctx) == {}


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
    assert "output up to the timeout" in out.text
    assert "FATAL-BEFORE-HANG" in out.text
    full = _spill_path(out.text).read_text(encoding="utf-8")
    assert "line 1500 of noise" in full
    assert full.rstrip().endswith("FATAL-BEFORE-HANG")
