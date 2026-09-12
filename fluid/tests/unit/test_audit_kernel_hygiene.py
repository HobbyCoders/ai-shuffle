"""Kernel hygiene fixes from the six-session code-mode audit.

The audit found ResultRef defeated in every session it looked at, and the
kernel's stdout handling amplifying the damage. Three behaviours are pinned
here:

- **Ref recovery.** Every ResultRef is filed in the namespace's ``_refs`` under
  a short id its ``repr`` prints, so a handle the cell dropped is recovered
  with ``_refs[7]`` instead of a re-run; the previous cell's displayed value
  stays bound to ``_``.
- **Fair-share stdout.** The cap is divided across the cell's prints instead of
  tail-dropped, so the summary a model prints *last* still arrives — and the
  notice names what was lost.
- **Committed-call manifest.** A cell that dies mid-way reports the calls that
  already landed, so completed Edits are not re-issued.
"""

import pytest

from fluid.codemode.kernel import HostReply
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.codemode.truncate import (
    DEFAULT_LIMIT,
    exceeds,
    fair_share_stdout,
    to_result_ref,
)
from fluid.codemode.vfs import RefRegistry, ResultRef

# ── pure policy: fair-share stdout ────────────────────────────────────────


def test_fair_share_keeps_a_piece_of_every_print() -> None:
    # 13 prints, the last two the heaviest — a tail-drop loses exactly them.
    segments = [f"print-{i}:" + ("x" * 1000) + "\n" for i in range(11)]
    segments.append("print-11:" + ("y" * 48_000) + "\n")
    segments.append("print-12:" + ("z" * 31_000) + "\n")
    total = sum(len(s) for s in segments)

    out = fair_share_stdout(segments, 16_384)

    # Something from every print survived, the last ones included.
    for i in range(13):
        assert f"print-{i}:" in out
    assert "yyy" in out and "zzz" in out
    # The notice names the totals and the two heaviest drops by print number.
    assert f"of {total} chars across 13 prints" in out
    assert "heaviest drops: print 12 (-" in out  # biggest loser named first
    assert "print 13 (-" in out
    # Payload stays inside the cap (the closing notice is metadata on top).
    payload, notice = out.rsplit("\n", 1)
    assert notice.startswith("…[stdout truncated:")
    assert len(payload) <= 16_384


def test_fair_share_is_a_noop_under_the_cap() -> None:
    segments = ["a\n", "b\n", "c\n"]
    assert fair_share_stdout(segments, 100) == "a\nb\nc\n"
    assert fair_share_stdout([], 100) == ""


def test_fair_share_small_prints_survive_whole() -> None:
    # One greedy print must not starve the eleven small ones: max-min fairness
    # gives the shorts their full length and the surplus to the hog.
    segments = ["tiny\n"] * 11 + ["H" * 50_000 + "\n"]
    out = fair_share_stdout(segments, 4_000)
    assert out.count("tiny") == 11
    assert "across 12 prints" in out


def test_fair_share_degenerate_share_still_marks_every_print() -> None:
    segments = [f"line {i} " + "q" * 200 + "\n" for i in range(300)]
    out = fair_share_stdout(segments, 1_000)
    assert "across 300 prints" in out
    # Every print is represented by at least an ellipsis marker.
    assert out.count("…") >= 300


# ── pure policy: ref threshold boundary ───────────────────────────────────


def test_ref_threshold_boundary_is_exact() -> None:
    assert DEFAULT_LIMIT == 8192
    at_limit = "a" * DEFAULT_LIMIT
    assert not exceeds(at_limit)
    assert not isinstance(to_result_ref(at_limit, ResultRef), ResultRef)
    over = "a" * (DEFAULT_LIMIT + 1)
    assert exceeds(over)
    assert isinstance(to_result_ref(over, ResultRef), ResultRef)
    # UTF-8 bytes, not characters: 8192 two-byte chars are over the limit.
    assert exceeds("é" * (DEFAULT_LIMIT // 2 + 1))


# ── pure policy: the registry itself ──────────────────────────────────────


def test_registry_keys_are_monotonic_and_bounded() -> None:
    registry = RefRegistry(max_entries=3)
    refs = [registry.register(ResultRef(f"payload-{i}")) for i in range(5)]
    assert [r.key for r in refs] == [1, 2, 3, 4, 5]
    assert registry.keys() == [3, 4, 5]  # FIFO eviction
    assert registry[5] == "payload-4"
    # An evicted key is never handed out again, and fails loudly.
    with pytest.raises(KeyError) as excinfo:
        registry[1]
    assert "no ResultRef #1" in str(excinfo.value)


def test_result_ref_repr_carries_its_key() -> None:
    registry = RefRegistry()
    for _ in range(6):
        registry.register(ResultRef("x" * 100))
    ref = registry.register(ResultRef("y" * 55_123))
    text = repr(ref)
    assert text.startswith("<ResultRef #7: 55123 bytes; preview=")
    assert len(text) < 400  # still a preview, not the payload
    # An unregistered ref keeps the original, key-less shape.
    assert repr(ResultRef("z" * 10)).startswith("<ResultRef 10 bytes;")


def test_result_ref_survives_pickle_with_its_key() -> None:
    import pickle

    ref = RefRegistry().register(ResultRef("payload"))
    clone = pickle.loads(pickle.dumps(ref))
    assert isinstance(clone, ResultRef) and clone == "payload"
    assert clone.key == ref.key


# ── kernel behaviour (subprocess) ─────────────────────────────────────────


def _blob_source(name: str = "blob") -> dict[str, str]:
    return {
        name: (
            f"def {name}(**kwargs) -> str:\n"
            f"    return __host_call__('mcp__s__{name}', kwargs)\n"
        )
    }


async def _big_host(name: str, kwargs: dict) -> HostReply:
    return HostReply(result="X" * 20_000)


async def test_dropped_ref_is_recoverable_from_the_registry() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.set_vfs(_blob_source())
        # The classic audit shape: the cell prints the repr and binds nothing.
        first = await kernel.exec(
            "from tools.blob import blob\nprint(repr(blob()))",
            timeout=20, on_host_call=_big_host,
        )
        assert first.error is None
        assert "<ResultRef #1: 20000 bytes" in first.stdout
        # A later cell recovers the payload by the id the repr printed — no
        # second host call.
        second = await kernel.exec(
            "len(_refs[1]), _refs[1][:3]", timeout=20, on_host_call=_big_host
        )
        assert second.error is None
        assert second.value_repr == "(20000, 'XXX')"
        assert second.host_calls == 0
    finally:
        await kernel.stop()


async def test_underscore_binds_the_previous_displayed_value() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.set_vfs(_blob_source())
        r1 = await kernel.exec("21 * 2", timeout=20, on_host_call=_big_host)
        assert r1.value_repr == "42"
        r2 = await kernel.exec("_ + 1", timeout=20, on_host_call=_big_host)
        assert r2.value_repr == "43"
        # An unbound tool call as the last expression is reachable too.
        r3 = await kernel.exec(
            "from tools.blob import blob\nblob()", timeout=20, on_host_call=_big_host
        )
        assert "ResultRef" in r3.value_repr
        r4 = await kernel.exec("len(_)", timeout=20, on_host_call=_big_host)
        assert r4.value_repr == "20000"
    finally:
        await kernel.stop()


async def test_refs_registry_survives_a_kernel_restart() -> None:
    """A restart rebuilds the registry and re-adopts every restored ref."""
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.set_vfs(_blob_source())
        await kernel.exec(
            "from tools.blob import blob\nkept = blob()",
            timeout=20, on_host_call=_big_host,
        )
        slow = await kernel.exec(
            "while True:\n    pass", timeout=0.4, on_host_call=_big_host
        )
        assert slow.error and "restored from last checkpoint" in slow.error
        after = await kernel.exec(
            "kept.key, len(_refs[kept.key])", timeout=20, on_host_call=_big_host
        )
        assert after.error is None
        assert after.value_repr == "(1, 20000)"
    finally:
        await kernel.stop()


async def test_cell_cannot_detach_the_ref_registry() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.set_vfs(_blob_source())
        await kernel.exec("_refs = {}", timeout=20, on_host_call=_big_host)
        r = await kernel.exec(
            "from tools.blob import blob\nblob()\nsorted(_refs.keys())",
            timeout=20, on_host_call=_big_host,
        )
        assert r.value_repr == "[1]"
    finally:
        await kernel.stop()


# ── committed-call manifest (host side) ───────────────────────────────────


class _MiniLoop:
    """The minimal ``loop`` surface ``CodeModeSession.run_python`` reads."""

    def __init__(self, registry) -> None:
        from fluid.permissions.engine import PermissionEngine
        from fluid.tools.registry import ToolCtx

        self.registry = registry
        self.ctx = ToolCtx()
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)

    async def _await_permission(self, *a, **k):  # pragma: no cover - never asked
        return "allow", None, None


def _edit_registry():
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    @tool("Read", "Read a file.",
          {"type": "object", "properties": {"file_path": {"type": "string"}},
           "required": ["file_path"]})
    async def read(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"body of {args['file_path']}"}]}

    @tool("Edit", "Edit a file.", risk="write",
          input_schema={"type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"]})
    async def edit(args: dict) -> dict:
        if args["file_path"].endswith("missing.py"):
            return {"content": [{"type": "text", "text": "no such file"}],
                    "is_error": True}
        return {"content": [{"type": "text", "text": f"edited {args['file_path']}"}]}

    return _as_builtins(ToolRegistry(), [read, edit])


def _as_builtins(registry, tools):
    """Round 9: in-process server errors are soft results in code mode, so
    the deliberately failing Edit below is registered as a BUILTIN, whose
    error still aborts the cell (what the manifest is for)."""
    from fluid.tools.registry import ToolOutcome, ToolSpec

    for t in tools:
        async def handler(input, ctx, _fn=t.handler):
            raw = await _fn(input)
            text = "".join(
                part.get("text", "") for part in raw.get("content", [])
                if isinstance(part, dict)
            )
            return ToolOutcome(text=text, is_error=bool(raw.get("is_error") or raw.get("isError")))
        registry.register_builtin(
            ToolSpec(name=t.name, description=t.description,
                     input_schema=t.input_schema, risk=t.risk),
            handler,
        )
    return registry


async def test_error_cell_reports_the_calls_that_already_landed() -> None:
    from fluid.codemode.session import CodeModeSession

    registry = _edit_registry()
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    code = (
        "from tools import Read, Edit\n"
        "Read(file_path='a.py')\n"
        "Edit(file_path='a.py')\n"
        "Edit(file_path='b.py')\n"
        "Edit(file_path='missing.py')\n"   # raises ToolError; the cell aborts
        "print('never reached')\n"
    )
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30}, loop=loop
        )
    finally:
        await session.aclose()

    assert outcome.is_error
    assert "never reached" not in outcome.text
    manifest = next(
        line for line in outcome.text.splitlines()
        if line.startswith("calls completed before the error:")
    )
    # Ordered, named by target, with the failure that ended the cell last.
    assert "1. Read(a.py) ok" in manifest
    assert "2. Edit(a.py) ok" in manifest
    assert "3. Edit(b.py) ok" in manifest
    assert "4. Edit(missing.py) FAILED" in manifest
    assert manifest.index("1. Read") < manifest.index("2. Edit")
    assert "already applied to disk; do not re-issue them" in manifest


async def test_clean_cell_carries_no_manifest() -> None:
    from fluid.codemode.session import CodeModeSession

    registry = _edit_registry()
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1",
            input={"code": "from tools import Read\nprint(Read(file_path='a.py'))\n",
                   "timeout": 30},
            loop=loop,
        )
        assert not outcome.is_error
        assert "calls completed before the error" not in outcome.text
        # An error with no completed calls says nothing either.
        broken = await session.run_python(
            sid="s1", turn_id="t2", input={"code": "1/0", "timeout": 30}, loop=loop
        )
        assert broken.is_error
        assert "calls completed before the error" not in broken.text
    finally:
        await session.aclose()


async def test_read_only_failure_omits_the_mutation_warning() -> None:
    from fluid.codemode.session import CodeModeSession

    registry = _edit_registry()
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    code = (
        "from tools import Read\n"
        "Read(file_path='a.py')\n"
        "undefined_name\n"
    )
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30}, loop=loop
        )
    finally:
        await session.aclose()
    assert outcome.is_error
    assert "1. Read(a.py) ok" in outcome.text
    assert "already applied to disk" not in outcome.text


async def test_kernel_stdout_cap_fair_shares_across_prints() -> None:
    kernel = SubprocessKernel(KernelConfig(stdout_cap=2_000))
    await kernel.start()
    try:
        r = await kernel.exec(
            "for i in range(5):\n"
            "    print('mark%d' % i, 'v' * 2000)\n"
            "print('FINAL SUMMARY')",
            timeout=20, on_host_call=_big_host,
        )
        assert r.error is None
        for i in range(5):
            assert f"mark{i}" in r.stdout
        # The thing printed last is exactly what the old tail-drop lost.
        assert "FINAL SUMMARY" in r.stdout
        assert "stdout truncated: kept" in r.stdout
        assert "across 6 prints" in r.stdout
    finally:
        await kernel.stop()


def test_fair_share_payload_never_exceeds_the_cap() -> None:
    # Thousands of prints with shares below two characters: an ellipsis per
    # print used to push the payload to 2.4x the cap.
    segments = [f"line {i}\n" for i in range(20_000)]
    cap = 16_384
    out = fair_share_stdout(segments, cap)
    payload = out.rsplit("\n", 1)[0]  # everything before the closing notice
    assert len(payload) <= cap
    assert "across 20000 prints" in out
    assert "…" in payload  # the drop is still visible
