"""Repeat reads in code mode come back as a pointer, not a second payload.

The audit's P1: a model re-reads a file it already has, the passive
"unchanged since your earlier read" note does nothing to stop it, and the
whole payload is paid for twice. In code mode the first oversized read is
already alive in the kernel behind a ``ResultRef`` with a registry id, so the
second read of the *same window of the same bytes* returns that id and its
size instead of the content.

A *different* window of the same unchanged file used to pay full price too —
the audit found the same file re-read six to eight times at different offsets.
So when the held ref covers the file whole, a windowed re-read is answered with
the slicing expression for that window instead of the lines.

Everything that breaks the "same bytes, already held" claim still has to fall
through to a real read: an edit to the file, a ref that aged out of the bounded
registry, a payload too small to have become a ref at all, and an earlier read
that only ever held a *window* (it cannot serve a window it never read). The
JSON tool surface never sees any of this — there is no kernel to hold the
payload, so its Read is exactly what it was.
"""

from pathlib import Path

from fluid.codemode.bridge import HostBridge
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import Projection
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel, _reply_frame
from fluid.codemode.truncate import repeat_read_pointer, windowed_read_pointer
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import read as read_tool
from fluid.tools.registry import ContentWindow, ToolCtx, ToolRegistry

# Comfortably past the 8192-byte ref threshold once line numbers are added.
_BIG = "".join(f"line {i} " + "s" * 60 + "\n" for i in range(200))


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(read_tool.SPEC, read_tool.handler)
    return registry


class _Kernel:
    """One kernel + one bridge over a real Read, for a whole test."""

    def __init__(self, cwd: Path) -> None:
        self._registry = _registry()
        self._bridge = HostBridge(
            self._registry,
            PermissionEngine("never_ask"),
            ctx=ToolCtx(cwd=str(cwd)),
            # Reading the same file twice is the whole point here; the
            # doom-loop guard would turn the second call into an "ask".
            doom_loop=False,
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


_IMPORT = "from tools.Read import Read\n"


# ── pure policy ───────────────────────────────────────────────────────────


def test_pointer_names_the_ref_and_the_size() -> None:
    text = repeat_read_pointer(7, 55123)
    assert "_refs[7]" in text
    assert "55,123 bytes" in text
    assert text.startswith("unchanged since your earlier read")


def test_windowed_pointer_spells_out_the_slice() -> None:
    text = windowed_read_pointer(3, 55123, 49, 199)
    assert "_refs[3].splitlines()[49:199]" in text
    assert "Lines 50-199" in text
    assert "55,123 bytes" in text
    assert text.startswith("unchanged since your earlier read")
    # An open-ended window keeps the slice open-ended rather than inventing a
    # last line the payload may not have.
    assert "_refs[3].splitlines()[49:]" in windowed_read_pointer(3, 10, 49, None)


def test_reply_frame_carries_content_id_only_on_success() -> None:
    assert _reply_frame(HostReply(result="x", content_id="cid"))["content_id"] == "cid"
    assert "content_id" not in _reply_frame(HostReply(result="x"))
    assert "content_id" not in _reply_frame(HostReply(error="boom", content_id="cid"))


def test_reply_frame_carries_the_window_claim() -> None:
    window = ContentWindow(content_id="whole", start=49, stop=199)
    frame = _reply_frame(HostReply(result="x", content_id="cid", window_of=window))
    assert frame["window_of"] == {"content_id": "whole", "start": 49, "stop": 199}
    assert "window_of" not in _reply_frame(HostReply(result="x"))
    assert "window_of" not in _reply_frame(HostReply(error="boom", window_of=window))


def test_content_id_tracks_window_and_file_version(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    stamp = read_tool._stamp(target)

    whole = read_tool._content_id(str(target), None, stamp)
    same = read_tool._content_id(str(target), None, stamp)
    window = read_tool._content_id(str(target), (5, 20), stamp)
    edited = read_tool._content_id(str(target), None, (stamp[0] + 1, stamp[1]))

    assert whole == same
    assert window != whole
    assert edited != whole
    # No stat, no identity claim — and no pointer substitution downstream.
    assert read_tool._content_id(str(target), None, None) is None


async def test_a_read_that_reaches_the_end_claims_the_whole_file_identity(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    registry = _registry()
    ctx = ToolCtx(cwd=str(tmp_path))

    default = await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    spelled = await registry.dispatch(
        "Read", {"file_path": str(target), "offset": 1, "limit": 5_000}, ctx
    )
    partial = await registry.dispatch(
        "Read", {"file_path": str(target), "offset": 50, "limit": 150}, ctx
    )

    # Same bytes returned, so the same identity — the window that produced them
    # is not part of the claim once the window covered everything.
    assert default.text.split("\n\n[note")[0] == spelled.text.split("\n\n[note")[0]
    assert default.content_id == spelled.content_id
    assert default.window_of is spelled.window_of is None
    # A read that skips the head is a window, and says which one.
    assert partial.content_id != default.content_id
    assert partial.window_of == ContentWindow(
        content_id=default.content_id, start=49, stop=199
    )


# ── code mode ─────────────────────────────────────────────────────────────


async def test_repeat_read_of_unchanged_file_returns_the_ref_pointer(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        first = await k.run(f"{_IMPORT}first = Read(file_path={path})\nfirst")
        assert first.error is None
        assert "<ResultRef #1:" in first.value_repr

        second = await k.run(f"{_IMPORT}Read(file_path={path})")
        assert second.error is None
        assert "unchanged since your earlier read" in second.value_repr
        assert "_refs[1]" in second.value_repr
        # The bytes stayed in the kernel: the pointer is a short plain string,
        # and the payload is still reachable under the id it names.
        assert "ResultRef" not in second.value_repr
        assert len(second.value_repr) < 300

        recovered = await k.run("len(_refs[1]) == len(first)")
        assert recovered.value_repr == "True"


async def test_changed_file_reads_in_full_again(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        # Request both versions whole: the appended line is beyond Read's
        # default page. This test is about stale-ref invalidation, not paging.
        await k.run(f"{_IMPORT}Read(file_path={path}, limit=300)")
        target.write_text(_BIG + "line 200 tail\n", encoding="utf-8")
        again = await k.run(f"{_IMPORT}Read(file_path={path}, limit=300)")

        assert again.error is None
        # A second, distinct ref — not a pointer at the stale first one.
        assert "<ResultRef #2:" in again.value_repr
        assert "unchanged since your earlier read" not in again.value_repr
        tail = await k.run("'line 200 tail' in _refs[2]")
        assert tail.value_repr == "True"


async def test_a_window_of_a_changed_file_is_not_sliced_from_the_stale_ref(
    tmp_path,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        target.write_text("line 0 rewritten\n" + _BIG, encoding="utf-8")
        windowed = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")

        assert windowed.error is None
        # Slicing the held ref would hand back lines the file no longer has at
        # those numbers, so the window is read for real.
        assert "unchanged since your earlier read" not in windowed.value_repr
        assert "<ResultRef #2:" in windowed.value_repr


async def test_a_new_window_is_sliced_out_of_the_whole_file_ref(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        windowed = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")

        assert windowed.error is None
        # The whole file is already in the kernel, so a new window costs a
        # slicing expression, not a second overlapping payload.
        assert "unchanged since your earlier read" in windowed.value_repr
        assert "_refs[1].splitlines()[49:199]" in windowed.value_repr
        assert "ResultRef" not in windowed.value_repr
        assert len(windowed.value_repr) < 300

        # And the spelled-out slice really is the window that was asked for.
        sliced = await k.run(
            "_w = _refs[1].splitlines()[49:199]\n"
            "(len(_w), _w[0].split('\\t')[0], _w[-1].split('\\t')[0])"
        )
        assert sliced.value_repr == "(150, '50', '199')"


async def test_a_window_held_only_in_part_still_reads_for_real(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        # The first read skips the head, so the ref holds a window, not the
        # file — it cannot answer for lines it never saw.
        first = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")
        assert "<ResultRef #1:" in first.value_repr

        other = await k.run(f"{_IMPORT}Read(file_path={path}, offset=1, limit=180)")
        assert other.error is None
        assert "unchanged since your earlier read" not in other.value_repr
        assert "<ResultRef #2:" in other.value_repr
        # Its own window repeated is still the cheap case, as before.
        again = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")
        assert "_refs[1]" in again.value_repr
        assert "splitlines()" not in again.value_repr


async def test_evicted_ref_falls_through_to_a_real_read(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))
    others = []
    for i in range(55):
        sibling = tmp_path / f"other{i}.txt"
        sibling.write_text(f"file {i}\n{_BIG}", encoding="utf-8")
        others.append(str(sibling))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        # Push the read's ref out of the bounded FIFO registry with other
        # oversized results.
        flood = await k.run(
            f"{_IMPORT}for _p in {others!r}:\n    Read(file_path=_p)\n1 in _refs"
        )
        assert flood.error is None
        assert flood.value_repr == "False"

        again = await k.run(f"{_IMPORT}Read(file_path={path})")
        assert again.error is None
        # Pointing at #1 would be pointing at nothing; a real read happened.
        assert "unchanged since your earlier read" not in again.value_repr
        assert "<ResultRef #" in again.value_repr


async def test_small_reads_never_become_pointers(tmp_path) -> None:
    target = tmp_path / "small.txt"
    target.write_text("just a couple of lines\nnothing more\n", encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        again = await k.run(f"{_IMPORT}Read(file_path={path})")

        assert again.error is None
        # Under the ref threshold there is nothing in the kernel to point at,
        # so the content comes back as it always did.
        assert "just a couple of lines" in again.value_repr
        assert "_refs[" not in again.value_repr


# ── JSON tool surface is untouched ────────────────────────────────────────


async def test_json_surface_repeat_read_still_returns_the_content(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    registry = _registry()
    ctx = ToolCtx(cwd=str(tmp_path))

    first = await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    second = await registry.dispatch("Read", {"file_path": str(target)}, ctx)

    assert "line 199" in first.text
    assert "line 199" in second.text  # full content, both times
    assert "_refs[" not in second.text
    assert "[note: unchanged since your earlier read" in second.text
    # The identity claim rides along for whoever can use it; it is the same
    # for both calls because nothing about the file moved.
    assert first.content_id == second.content_id is not None
