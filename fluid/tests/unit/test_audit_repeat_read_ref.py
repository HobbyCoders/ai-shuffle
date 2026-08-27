"""Repeat reads in code mode come back as a pointer, not a second payload.

The audit's P1: a model re-reads a file it already has, the passive
"unchanged since your earlier read" note does nothing to stop it, and the
whole payload is paid for twice. In code mode the first oversized read is
already alive in the kernel behind a ``ResultRef`` with a registry id, so the
second read of the *same window of the same bytes* returns that id and its
size instead of the content.

Everything that breaks the "same bytes" claim has to fall through to a real
read: an edit to the file, a different offset/limit window, a ref that aged
out of the bounded registry, and a payload too small to have become a ref at
all. The JSON tool surface never sees any of this — there is no kernel to hold
the payload, so its Read is exactly what it was.
"""

from pathlib import Path

from fluid.codemode.bridge import HostBridge
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import Projection
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel, _reply_frame
from fluid.codemode.truncate import repeat_read_pointer
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import read as read_tool
from fluid.tools.registry import ToolCtx, ToolRegistry

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


def test_reply_frame_carries_content_id_only_on_success() -> None:
    assert _reply_frame(HostReply(result="x", content_id="cid"))["content_id"] == "cid"
    assert "content_id" not in _reply_frame(HostReply(result="x"))
    assert "content_id" not in _reply_frame(HostReply(error="boom", content_id="cid"))


def test_content_id_tracks_window_and_file_version(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    stamp = read_tool._stamp(target)

    whole = read_tool._content_id((str(target), 1, None), stamp)
    same = read_tool._content_id((str(target), 1, None), stamp)
    window = read_tool._content_id((str(target), 5, 20), stamp)
    edited = read_tool._content_id((str(target), 1, None), (stamp[0] + 1, stamp[1]))

    assert whole == same
    assert window != whole
    assert edited != whole
    # No stat, no identity claim — and no pointer substitution downstream.
    assert read_tool._content_id((str(target), 1, None), None) is None


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
        await k.run(f"{_IMPORT}Read(file_path={path})")
        target.write_text(_BIG + "line 200 tail\n", encoding="utf-8")
        again = await k.run(f"{_IMPORT}Read(file_path={path})")

        assert again.error is None
        # A second, distinct ref — not a pointer at the stale first one.
        assert "<ResultRef #2:" in again.value_repr
        assert "unchanged since your earlier read" not in again.value_repr
        tail = await k.run("'line 200 tail' in _refs[2]")
        assert tail.value_repr == "True"


async def test_a_different_window_is_not_the_same_payload(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        windowed = await k.run(f"{_IMPORT}Read(file_path={path}, offset=50, limit=150)")

        assert windowed.error is None
        assert "unchanged since your earlier read" not in windowed.value_repr
        assert "<ResultRef #2:" in windowed.value_repr


async def test_evicted_ref_falls_through_to_a_real_read(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(_BIG, encoding="utf-8")
    path = repr(str(target))

    async with _Kernel(tmp_path) as k:
        await k.run(f"{_IMPORT}Read(file_path={path})")
        # Push the read's ref out of the bounded FIFO registry with other
        # oversized results.
        flood = await k.run(
            f"{_IMPORT}"
            f"for _i in range(55):\n    Read(file_path={path}, offset=1, limit=1000 + _i)\n"
            "1 in _refs"
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
