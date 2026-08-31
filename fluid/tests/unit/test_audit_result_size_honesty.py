"""Result-size honesty: what the harness says it kept, it actually kept.

Audit item 8. Six defects with one shape — the harness drops or withholds
content and then describes the drop in a way that costs a paid re-fetch:

* a model that always reads with offset/limit never hit the repeat-read
  dedupe at all, because a windowed read filed nothing under the file's own
  identity (the same file was read four times in one session);
* the unchanged-read note claimed "you already have this" about a payload the
  model demonstrably never saw — a ResultRef stub a cell only measured;
* the stdout cap named the damage but no way back to it, so the recovery was a
  re-run of the tools whose full results were sitting in the kernel;
* a consumed image was replaced by a placeholder advising HistoryExpand, which
  recovers text and cannot restore an image — the model designed from memory;
* a read past EOF said "(empty file)", which reads as a bug in the file;
* an identical repeat search returned every row a second time and then said
  the rows were already above.
"""

from pathlib import Path

from fluid.codemode import _runner
from fluid.codemode.bridge import HostBridge
from fluid.codemode.projection import Projection
from fluid.codemode.session import _run_python_desc
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.codemode.truncate import (
    DEFAULT_STDOUT_CAP,
    fair_share_stdout,
    windowed_read_pointer,
)
from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.recall import expand_ref
from fluid.context.trim import ToolResultTrimmer, TrimConfig, image_source_path
from fluid.engine.background import BackgroundLifecycleMixin
from fluid.events.ops import BackgroundTaskFinished
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import SessionNode
from fluid.tools.builtins import grep as grep_tool
from fluid.tools.builtins import read as read_tool
from fluid.tools.registry import ToolCtx, ToolRegistry

# Comfortably past the 8192-byte ref threshold once line numbers are added.
_BIG = "".join(f"line {i} " + "s" * 60 + "\n" for i in range(400))
_IMPORT = "from tools.Read import Read\n"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(read_tool.SPEC, read_tool.handler)
    registry.register_builtin(grep_tool.SPEC, grep_tool.handler)
    return registry


class _Kernel:
    """One kernel + one bridge over a real Read, for a whole test."""

    def __init__(self, cwd: Path) -> None:
        self._registry = _registry()
        self._bridge = HostBridge(
            self._registry,
            PermissionEngine("never_ask"),
            ctx=ToolCtx(cwd=str(cwd)),
            doom_loop=False,  # re-reading the same file is the point here
            result_budget=KernelConfig().ref_threshold,
        )
        self._kernel = SubprocessKernel(KernelConfig())

    async def __aenter__(self):
        await self._kernel.start()
        await self._kernel.set_vfs(Projection(self._registry.list_tools()).sources())
        return self

    async def __aexit__(self, *exc) -> None:
        await self._kernel.stop()

    async def run(self, code: str):
        return await self._kernel.exec(
            code, timeout=30, on_host_call=self._bridge.on_host_call
        )


def _node(node_id: str, role: str, content: list) -> SessionNode:
    return SessionNode(
        node_id=node_id,
        parent_id=None,
        message=Message(role=role, content=content),
    )


# ── 1. a windowed read files the whole file's identity too ────────────────


def test_containment_is_what_a_held_window_can_answer_for() -> None:
    # A ref holding the payload whole answers for any window of it.
    assert _runner._contains(None, 500, 900)
    assert _runner._slice_base(None, 500, 900) == 0
    # A ref holding lines 50-200 answers for windows inside that span, with
    # slice indices relative to its own start.
    assert _runner._contains((49, 199), 59, 109)
    assert _runner._slice_base((49, 199), 59, 109) == 49
    # And refuses anything reaching outside it — in either direction.
    assert not _runner._contains((49, 199), 0, 100)
    assert not _runner._contains((49, 199), 150, 300)
    assert _runner._slice_base((49, 199), 0, 100) is None


def test_windowed_pointer_indices_are_relative_to_the_held_window() -> None:
    whole = windowed_read_pointer(2, 9_000, 59, 109)
    assert "_refs[2].splitlines()[59:109]" in whole
    assert "the whole file is already in the kernel" in whole

    partial = windowed_read_pointer(2, 9_000, 59, 109, offset=49)
    # Same file lines, but the ref starts at line 50, so the slice shifts.
    assert "Lines 60-109" in partial
    assert "_refs[2].splitlines()[10:60]" in partial
    assert "an earlier read covering these lines" in partial


async def test_a_window_inside_an_earlier_window_is_sliced_not_re_read(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        # A model that never issues a bare Read — the case where the dedupe
        # used to be structurally dead.
        first = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")
        assert "<ResultRef #1:" in first.value_repr

        inner = await k.run(f"{_IMPORT}Read(file_path={path}, offset=60, limit=50)")
        assert inner.error is None
        assert "unchanged since your earlier read" in inner.value_repr
        assert "_refs[1].splitlines()[10:60]" in inner.value_repr
        assert "ResultRef" not in inner.value_repr

        # The spelled-out slice really is the window that was asked for.
        sliced = await k.run(
            "_w = _refs[1].splitlines()[10:60]\n"
            "(len(_w), _w[0].split('\\t')[0], _w[-1].split('\\t')[0])"
        )
        assert sliced.value_repr == "(50, '60', '109')"


async def test_an_edit_invalidates_the_registered_whole_file_identity(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")
        target.write_text("rewritten head\n" + _BIG, encoding="utf-8")

        again = await k.run(f"{_IMPORT}Read(file_path={path}, offset=60, limit=50)")
        assert again.error is None
        # Those line numbers mean something else now, so the window is read.
        assert "unchanged since your earlier read" not in again.value_repr
        assert "_refs[" not in again.value_repr
        assert "line 58" in again.value_repr  # shifted by the inserted head


async def test_a_whole_file_read_is_not_answered_from_a_partial_ref(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")
        whole = await k.run(f"{_IMPORT}Read(file_path={path})")

        assert whole.error is None
        # The held ref has lines 50-199 and nothing else; claiming it holds the
        # file would be the same lie the placeholder bugs are about.
        assert "unchanged since your earlier read" not in whole.value_repr
        assert "<ResultRef #2:" in whole.value_repr

        # …and now the whole file IS held, so the next window is free again.
        after = await k.run(f"{_IMPORT}Read(file_path={path}, offset=60, limit=50)")
        assert "_refs[2].splitlines()[59:109]" in after.value_repr


# ── 2. the note only fires on content that reached the model ──────────────


async def test_unchanged_note_is_withheld_when_the_payload_never_landed(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    registry = _registry()
    # A surface that hands anything over 8k to the cell as a ResultRef stub.
    ctx = ToolCtx(cwd=str(tmp_path), result_budget=8_192)

    call = {"file_path": str(target), "offset": 50, "limit": 150}
    await registry.dispatch("Read", dict(call), ctx)
    second = await registry.dispatch("Read", dict(call), ctx)

    assert "unchanged since your earlier read" not in second.text
    assert "line 50" in second.text  # the content comes back for real


async def test_unchanged_note_still_fires_on_a_payload_the_model_saw(
    tmp_path,
) -> None:
    target = tmp_path / "small.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry = _registry()
    ctx = ToolCtx(cwd=str(tmp_path), result_budget=8_192)

    await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    second = await registry.dispatch("Read", {"file_path": str(target)}, ctx)

    # Small enough to have been delivered whole, so the claim is true.
    assert "unchanged since your earlier read" in second.text


async def test_a_write_clears_the_delivered_mark(tmp_path) -> None:
    target = tmp_path / "small.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    registry = _registry()
    ctx = ToolCtx(cwd=str(tmp_path), result_budget=8_192)

    await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    target.write_text(_BIG, encoding="utf-8")  # now over the budget
    second = await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    third = await registry.dispatch("Read", {"file_path": str(target)}, ctx)

    assert "unchanged since your earlier read" not in second.text
    assert "unchanged since your earlier read" not in third.text


async def test_the_json_surface_keeps_its_note(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    registry = _registry()
    ctx = ToolCtx(cwd=str(tmp_path))  # no budget: results are delivered whole

    await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    second = await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    assert "unchanged since your earlier read" in second.text


# ── 3. the stdout cap names the budget and the handles ────────────────────


def test_truncation_notice_names_the_budget_and_the_kernel_handles() -> None:
    segments = ["x" * 5_000, "y" * 5_000]
    out = fair_share_stdout(segments, 2_000, refs=[(4, 27_696), (5, 1_200)])

    assert "one cell's stdout budget is 2,000 chars" in out
    assert "_refs[4] (27,696 bytes)" in out
    assert "_refs[5] (1,200 bytes)" in out
    assert "slice it instead of re-running" in out


def test_truncation_notice_without_handles_does_not_invent_one() -> None:
    out = fair_share_stdout(["x" * 5_000], 500)
    assert "Nothing in this cell is held as a ResultRef" in out
    assert "_refs[" not in out


def test_untruncated_stdout_carries_no_notice() -> None:
    assert fair_share_stdout(["short\n"], 1_000, refs=[(1, 10)]) == "short\n"


async def test_a_truncated_cell_points_at_its_own_result_ref(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        result = await k.run(f"{_IMPORT}print(Read(file_path={path}))")
        assert result.error is None
        assert "stdout truncated" in result.stdout
        # The full payload never left the kernel, so the recovery is a slice.
        assert "_refs[1]" in result.stdout
        assert "slice it instead of re-running" in result.stdout


def test_run_python_documents_the_stdout_budget() -> None:
    assert f"{DEFAULT_STDOUT_CAP:,} characters" in _run_python_desc()
    assert "16,384 characters" in _run_python_desc(16_384)
    assert "4,000 characters" in _run_python_desc(4_000)


# ── 4. images say what images need ────────────────────────────────────────


def _image_path() -> list[SessionNode]:
    return [
        _node("node_aa0", "user", [Text("look")]),
        _node("node_aa1", "assistant", [
            Text("reading"),
            ToolUseBlock(id="call0", name="Read", input={}),
        ]),
        _node("node_aa2", "user", [ToolResultBlock(
            tool_use_id="call0",
            content=[
                Text("[image: /work/shot.png, image/png, 4211 bytes]"),
                Image(media_type="image/png", data="a" * 20_000),
            ],
        )]),
        _node("node_aa3", "assistant", [Text("I see it")]),
    ]


def test_read_captions_an_image_with_its_full_path(tmp_path) -> None:
    # The caption is the only place the path survives once the image itself is
    # trimmed, so the placeholder can only name a re-Read target if it is here.
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    outcome = read_tool._read_image(shot, "image/png")
    assert str(shot) in outcome.text
    assert image_source_path(outcome.text) == str(shot)


def test_consumed_image_placeholder_names_the_re_read_not_history_expand() -> None:
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=10_000_000))
    rendered = trimmer.apply(_image_path())
    text = "".join(
        inner.text
        for message in rendered
        for block in message.content
        for inner in getattr(block, "content", None) or []
        if isinstance(inner, Text)
    )

    assert "image consumed from context" in text
    assert "HistoryExpand cannot restore it" in text
    assert 'Re-Read the file at "/work/shot.png"' in text


def test_a_pathless_image_says_re_run_the_tool() -> None:
    nodes = _image_path()
    nodes[2] = _node("node_aa2", "user", [ToolResultBlock(
        tool_use_id="call0",
        content=[Text("screenshot taken"), Image(media_type="image/png", data="a" * 20_000)],
    )])
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=10_000_000))
    rendered = trimmer.apply(nodes)
    text = "".join(
        inner.text
        for message in rendered
        for block in message.content
        for inner in getattr(block, "content", None) or []
        if isinstance(inner, Text)
    )
    assert "Re-run the tool that produced it" in text
    assert "Re-Read the file at" not in text


def test_a_consume_once_trim_is_reported_not_silent() -> None:
    compactor = Compactor(config=CompactionConfig())
    _rendered, promotions = compactor.apply(_image_path())

    assert [(p.tier, p.reason) for p in promotions] == [(1, "image_consumed")]
    assert promotions[0].trimmed == (("node_aa2", "call0"),)
    assert promotions[0].reclaimed_chars > 0
    # Sticky: the same render again is not a second event.
    _rendered, again = compactor.apply(_image_path())
    assert again == []


def test_history_expand_says_it_cannot_bring_an_image_back() -> None:
    text, is_error = expand_ref(_image_path(), "node_aa2")

    assert not is_error
    assert "image NOT restored here" in text
    assert "HistoryExpand recovers text only" in text
    assert 're-Read "/work/shot.png"' in text


# ── 5. a read past the end says so ────────────────────────────────────────


async def test_reading_past_the_end_names_the_length(tmp_path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch(
        "Read", {"file_path": str(target), "offset": 400}, ctx
    )
    assert "offset 400 is past the end of the file (3 lines)" in out.text
    assert "empty file" not in out.text


async def test_a_genuinely_empty_file_still_says_empty(tmp_path) -> None:
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    assert out.text.startswith("(empty file)")


# ── 6. an identical search is replaced, not repeated ──────────────────────


async def test_a_repeat_grep_returns_a_pointer_instead_of_the_rows(
    tmp_path,
) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "needle", "path": str(tmp_path)}

    first = await registry.dispatch("Grep", dict(call), ctx)
    second = await registry.dispatch("Grep", dict(call), ctx)

    assert "a.py" in first.text
    assert "a.py" not in second.text
    assert "identical search already run this session" in second.text
    assert "not repeated here" in second.text
    assert len(second.text) < len(first.text)
    # The identity claim goes with the payload it described.
    assert second.content_id is None


# ── 7. a background report is reachable without HistoryExpand ─────────────


def _wake(**kwargs) -> str:
    return BackgroundLifecycleMixin._background_wake_content(
        [BackgroundTaskFinished(task_id="t1", status="finished", **kwargs)]
    )


def test_background_completion_carries_the_report_path() -> None:
    text = _wake(summary="found three call sites", report_path="/tmp/report.md")
    assert "found three call sites" in text
    assert "[full report: /tmp/report.md — Read it for detail.]" in text


def test_a_long_report_is_headed_and_pointed_at_never_just_cut() -> None:
    text = _wake(summary="R" * 12_000, report_path="/tmp/report.md")

    assert "report head only — 12000 chars in full" in text
    assert "/tmp/report.md" in text
    assert "HistoryExpand cannot help here" in text
    assert len(text) < 6_000


def test_a_summary_with_no_report_path_is_never_elided() -> None:
    # Nothing to point at means nothing may be dropped — an elided summary
    # with no recovery handle is the defect, not the fix.
    text = _wake(summary="S" * 12_000)
    assert "S" * 12_000 in text
    assert "report head only" not in text
