"""Continual harness + hardened kernel boundaries.

Covers the three changes on top of M5 code-mode:

1. the filesystem guard (direct ``open``/``io``/``pathlib`` I/O blocked in the
   kernel; ``allow_fs`` opt-in restores it);
2. the memory cap on Windows (Job Object; the POSIX rlimit path is covered by
   platform behavior, not asserted here);
3. the continual harness — SkillStore CRUD + validation, the ``skills`` VFS
   mount (including guarded builtins for skill modules), the gated
   ``save_skill`` round-trip, session journaling, and the Synthesizer applied
   against a scripted fake provider.
"""

import sys

import pytest

from fluid.codemode import (
    HostBridge,
    KernelConfig,
    Projection,
    SkillStore,
    SubprocessKernel,
    Synthesizer,
    validate_skill,
)
from fluid.codemode.kernel import HostReply
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.codemode.skills import referenced_skills
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import StreamChunk
from fluid.tools.registry import ToolCtx, ToolRegistry


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


async def _exec(code: str, *, config: KernelConfig | None = None,
                skills: dict[str, str] | None = None, timeout: float = 8):
    kernel = SubprocessKernel(config)
    await kernel.start()
    try:
        if skills is not None:
            await kernel.set_vfs({}, skills)
        return await kernel.exec(code, timeout=timeout, on_host_call=_no_tools)
    finally:
        await kernel.stop()


# ─── filesystem guard ────────────────────────────────────────────────────────


async def test_direct_open_is_blocked() -> None:
    result = await _exec("open('x.txt', 'w')")
    assert result.error is not None
    assert "PermissionError" in result.error
    assert "file I/O is blocked" in result.error


async def test_io_module_routes_are_blocked() -> None:
    for snippet in ("import io\nio.open('x')",
                    "import io\nio.FileIO('x')",
                    "import io\nio.open_code('x')"):
        result = await _exec(snippet)
        assert result.error is not None and "PermissionError" in result.error


async def test_pathlib_io_is_blocked_but_path_math_works() -> None:
    result = await _exec("import pathlib\npathlib.Path('x.txt').read_text()")
    assert result.error is not None and "PermissionError" in result.error
    ok = await _exec("import pathlib\npathlib.Path('a/b').name")
    assert ok.error is None and ok.value_repr == "'b'"


async def test_allow_fs_optin_restores_open(tmp_path) -> None:
    target = tmp_path / "out.txt"
    code = (
        f"p = {str(target)!r}\n"
        "with open(p, 'w') as fh:\n"
        "    fh.write('ok')\n"
        "open(p).read()\n"
    )
    result = await _exec(code, config=KernelConfig(allow_fs=True))
    assert result.error is None
    assert result.value_repr == "'ok'"


# ─── memory cap (Windows Job Object; POSIX uses in-child rlimit) ─────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object path is win32-only")
async def test_windows_memory_cap_enforced() -> None:
    config = KernelConfig(mem_limit_bytes=256 * 1024 * 1024)
    result = await _exec("b = bytearray(512 * 1024 * 1024)\nlen(b)", config=config)
    # Allocation beyond the job cap fails (MemoryError) or kills the child.
    assert result.error is not None
    assert "MemoryError" in result.error or "died" in result.error


# ─── SkillStore ──────────────────────────────────────────────────────────────

_GREET = '"""Say hi to someone."""\n\ndef greet(name):\n    return "hi " + name\n'


def test_skill_validation() -> None:
    assert validate_skill("greet", _GREET) is None
    assert "invalid skill name" in validate_skill("Bad-Name", _GREET)
    assert "invalid skill name" in validate_skill("class", _GREET)
    assert "empty" in validate_skill("x", "   ")
    assert "does not parse" in validate_skill("x", "def broken(:\n")
    assert "docstring" in validate_skill("x", "def f():\n    return 1\n")


def test_store_crud_and_usage(tmp_path) -> None:
    store = SkillStore(tmp_path)
    saved = store.save("greet", _GREET)
    assert saved.description == "Say hi to someone."
    assert [s.name for s in store.list()] == ["greet"]
    assert store.sources() == {"greet": _GREET}

    store.note_uses(["greet", "unknown"])
    store.note_uses(["greet"])
    assert store.get("greet").uses == 2

    with pytest.raises(ValueError):
        store.save("greet", "not a docstring'd module ==")
    assert store.delete("greet") is True
    assert store.delete("greet") is False
    assert store.list() == []


def test_referenced_skills() -> None:
    code = (
        "from skills.greet import greet\n"
        "import json\n"
        "skills.fetch_all(x)\n"
        "greet('bob')\n"
    )
    assert referenced_skills(code) == ["greet", "fetch_all"]
    assert referenced_skills("print('skills')") == []
    # package form: the imported names, not aliases
    assert referenced_skills("from skills import greet, fetch_all as fa\nfa()") == [
        "greet", "fetch_all",
    ]
    assert referenced_skills("from skills import (\n    greet,\n)") == ["greet"]


def test_journal_and_cursor(tmp_path) -> None:
    store = SkillStore(tmp_path)
    for i in range(3):
        store.journal_append({"code": f"cell {i}"})
    assert store.journal_count() == 3
    assert [e["code"] for e in store.journal_entries(1)] == ["cell 1", "cell 2"]
    assert store.synth_cursor() == 0
    store.set_synth_cursor(3)
    assert store.synth_cursor() == 3


# ─── skills VFS mount ────────────────────────────────────────────────────────


async def test_skill_imports_and_persists_across_cells(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("greet", _GREET)
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs({}, store.sources())
        r1 = await kernel.exec(
            "from skills.greet import greet\ngreet('bob')",
            timeout=8, on_host_call=_no_tools,
        )
        assert r1.error is None and r1.value_repr == "'hi bob'"
        r2 = await kernel.exec("greet('ann')", timeout=8, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "'hi ann'"
    finally:
        await kernel.stop()


async def test_skill_modules_run_under_guarded_builtins(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("sneaky", '"""Sneaky."""\n\ndef sneaky():\n    return open("x")\n')
    store.save("importer", '"""Importer."""\nimport os\n\ndef importer():\n    return 1\n')
    result = await _exec(
        "from skills.sneaky import sneaky\nsneaky()", skills=store.sources()
    )
    assert result.error is not None and "PermissionError" in result.error
    result = await _exec("import skills.importer", skills=store.sources())
    assert result.error is not None and "blocked" in result.error


# ─── save_skill round-trip through the host gate ─────────────────────────────


async def test_save_skill_from_inside_the_kernel(tmp_path) -> None:
    store = SkillStore(tmp_path)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)
    bridge = HostBridge(registry, PermissionEngine("never_ask"))

    specs = [s for s in registry.list_tools() if s.source != "codemode"]
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(specs).sources(), store.sources())
        code = (
            "from tools.save_skill import save_skill\n"
            "save_skill(name='double', source='\"\"\"Double a number.\"\"\"\\n"
            "def double(x):\\n    return 2 * x\\n')\n"
        )
        result = await kernel.exec(code, timeout=8, on_host_call=bridge.on_host_call)
        assert result.error is None, result.error
        assert "saved" in result.value_repr

        saved = store.get("double")
        assert saved is not None and saved.description == "Double a number."

        # Next VFS sync mounts it (what CodeModeSession._ensure_kernel does).
        await kernel.set_vfs(Projection(specs).sources(), store.sources())
        r2 = await kernel.exec(
            "from skills.double import double\ndouble(21)",
            timeout=8, on_host_call=bridge.on_host_call,
        )
        assert r2.error is None and r2.value_repr == "42"
    finally:
        await kernel.stop()


async def test_save_skill_rejects_invalid_source(tmp_path) -> None:
    store = SkillStore(tmp_path)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    specs = [s for s in registry.list_tools() if s.source != "codemode"]
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(specs).sources())
        result = await kernel.exec(
            "from tools.save_skill import save_skill\n"
            "save_skill(name='bad', source='def f(): return 1')\n",
            timeout=8, on_host_call=bridge.on_host_call,
        )
        # Validation failure comes back as a ToolError raised in the cell.
        assert result.error is not None and "docstring" in result.error
        assert store.get("bad") is None
    finally:
        await kernel.stop()


# ─── session journaling (run_python drives the kernel) ───────────────────────


class _FakeLoop:
    permissions = None
    hooks = None
    ctx = ToolCtx()

    async def _emit(self, event) -> None:
        pass

    async def _await_permission(self, *args):  # pragma: no cover - never_ask
        return "allow", None, None


async def test_run_python_journals_cells(tmp_path) -> None:
    store = SkillStore(tmp_path)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)
    try:
        outcome = await session.run_python(
            sid="s", turn_id="t", input={"code": "print('hi')\n1 + 1"}, loop=_FakeLoop()
        )
        assert not outcome.is_error and "=> 2" in outcome.text
        entries = store.journal_entries()
        assert len(entries) == 1
        assert entries[0]["code"] == "print('hi')\n1 + 1"
        assert entries[0]["stdout"] == "hi\n"
        assert entries[0]["value"] == "2"
    finally:
        await session.aclose()


async def test_list_and_docs_include_skills(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("greet", _GREET)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    listing = session.render_list()
    assert "greet: Say hi to someone." in listing
    assert session.render_docs("greet") == _GREET
    assert session.render_docs("skills.greet") == _GREET
    assert "greet" in session.render_search("hi someone")


# ─── Synthesizer ─────────────────────────────────────────────────────────────


class _FakeProvider:
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        yield StreamChunk(kind="text_delta", payload={"text": self._text})
        yield StreamChunk(kind="message_stop")


def _journal(store: SkillStore, n: int) -> None:
    for i in range(n):
        store.journal_append({"code": f"x = {i}", "host_calls": 0})


async def test_synthesizer_saves_parsed_skills(tmp_path) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 3)
    reply = (
        'Some preamble.\n<skill name="double">\n'
        '"""Double a number."""\n\ndef double(x):\n    return 2 * x\n'
        "</skill>\n"
    )
    synth = Synthesizer(_FakeProvider(reply), "test-model", store, min_new_entries=2)
    assert synth.due()
    saved = await synth.run()
    assert saved == ["double"]
    assert store.get("double").description == "Double a number."
    assert store.synth_cursor() == 3
    assert not synth.due()


async def test_synthesizer_no_changes_and_invalid_skill(tmp_path) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 2)
    synth = Synthesizer(_FakeProvider("NO_CHANGES"), "m", store, min_new_entries=1)
    assert await synth.run() == []
    assert store.synth_cursor() == 2

    _journal(store, 2)
    bad = '<skill name="nodoc">\ndef nodoc():\n    return 1\n</skill>'
    synth2 = Synthesizer(_FakeProvider(bad), "m", store, min_new_entries=1)
    assert await synth2.run() == []
    assert synth2.last_error is not None and "docstring" in synth2.last_error
    assert store.synth_cursor() == 4  # cursor still advances past a bad slice


async def test_synthesizer_delete_directive(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("stale", _GREET.replace("greet", "stale"))
    _journal(store, 1)
    synth = Synthesizer(
        _FakeProvider('<delete-skill name="stale"/>'), "m", store, min_new_entries=1
    )
    assert await synth.run() == ["-stale"]
    assert store.get("stale") is None


async def test_synthesizer_prompt_contains_library_and_journal(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("greet", _GREET)
    store.journal_append({"code": "greeting = 1", "tools": ["mcp__wx__get_weather"]})
    provider = _FakeProvider("NO_CHANGES")
    synth = Synthesizer(provider, "m", store, min_new_entries=1)
    await synth.run()
    request = provider.requests[0]
    prompt = request.messages[0].content[0].text
    assert "skills.greet" in prompt
    assert "greeting = 1" in prompt
    assert "get_weather" in prompt
    assert request.tools == []


async def test_drain_is_bounded_when_the_model_stalls(tmp_path) -> None:
    """Session close must not hang on a synthesis pass that never returns."""
    import asyncio

    from fluid.codemode.skills import SkillStore

    class _StalledProvider:
        async def stream(self, request):
            await asyncio.sleep(3600)
            yield None  # pragma: no cover

    store = SkillStore(tmp_path)
    store.journal_append({"kind": "cell", "code": "print(1)", "stdout": "1\n"})
    synth = Synthesizer(
        _StalledProvider(), "m", store, min_new_entries=1,
        pass_timeout=0.2, close_timeout=0.5,
    )
    assert synth.due()
    await asyncio.wait_for(synth.drain(), timeout=5)
    assert "exceeded" in (synth.last_error or "")
