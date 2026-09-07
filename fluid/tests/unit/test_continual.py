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

import json
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


# ─── bounded storage: the harness reaps, the model never does ────────────────


def _read_journal_codes(store: SkillStore) -> list[str]:
    return [e["code"] for e in store.journal_entries(0)]


async def test_synthesis_reaps_consumed_journal_and_rebases_cursor(
    tmp_path, monkeypatch
) -> None:
    """A pass drops the consumed prefix past the tail and moves the cursor with it."""
    from fluid.codemode import skills as skills_mod

    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 4)
    store = SkillStore(tmp_path)
    _journal(store, 12)
    synth = Synthesizer(_FakeProvider("NO_CHANGES"), "m", store, min_new_entries=1)
    assert await synth.run() == []

    # 12 consumed, 4 kept as the tail → 8 lines dropped, cursor rebased by 8.
    assert store.journal_count() == 4
    assert store.synth_cursor() == 4
    assert _read_journal_codes(store) == [f"x = {i}" for i in range(8, 12)]
    # Nothing unconsumed is left, so the slice at the cursor is empty...
    assert store.journal_entries(store.synth_cursor()) == []
    assert not synth.due()
    # ...and new entries still land exactly at the cursor.
    _journal(store, 2)
    assert [e["code"] for e in store.journal_entries(store.synth_cursor())] == [
        "x = 0", "x = 1",
    ]


def test_reap_journal_never_drops_unconsumed_entries(tmp_path, monkeypatch) -> None:
    from fluid.codemode import skills as skills_mod

    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 2)
    store = SkillStore(tmp_path)
    _journal(store, 10)
    store.set_synth_cursor(3)
    assert store.reap_journal() == 3  # only the consumed prefix goes
    assert store.synth_cursor() == 0
    assert _read_journal_codes(store) == [f"x = {i}" for i in range(3, 10)]
    # Cursor at 0 → nothing droppable, so a second reap is a no-op.
    assert store.reap_journal() == 0


def test_reap_journal_drops_the_whole_prefix_past_the_byte_cap(
    tmp_path, monkeypatch
) -> None:
    from fluid.codemode import skills as skills_mod

    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 500)
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", 64)
    store = SkillStore(tmp_path)
    _journal(store, 10)
    store.set_synth_cursor(10)
    assert store.reap_journal() == 10  # tail budget yields to the size cap
    assert store.journal_count() == 0
    assert store.synth_cursor() == 0


def test_reap_journal_is_a_noop_on_a_quiet_store(tmp_path) -> None:
    store = SkillStore(tmp_path)
    assert store.reap_journal() == 0  # no journal file at all
    _journal(store, 5)
    store.set_synth_cursor(5)
    assert store.reap_journal() == 0  # under both caps → no rewrite
    assert store.journal_count() == 5


def test_journal_count_is_cached_and_self_heals(tmp_path) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 3)
    assert store.journal_count() == 3
    # Cache is live: the count came from memory, validated against the size.
    assert store._journal_bytes == (tmp_path / "journal.jsonl").stat().st_size
    _journal(store, 2)
    assert store.journal_count() == 5

    # Another writer (another process, another store handle) drifts the file.
    other = SkillStore(tmp_path)
    other.journal_append({"code": "x = elsewhere"})
    assert store.journal_count() == 6
    # An external rewrite shrinks it; the count heals on the next read.
    (tmp_path / "journal.jsonl").write_text(
        '{"code": "only one"}\n', encoding="utf-8"
    )
    assert store.journal_count() == 1


async def test_synthesis_log_rotates(tmp_path, monkeypatch) -> None:
    from fluid.codemode import continual as continual_mod

    monkeypatch.setattr(continual_mod, "MAX_LOG_BYTES", 200)
    monkeypatch.setattr(continual_mod, "MAX_LOG_ENTRIES", 5)
    store = SkillStore(tmp_path)
    synth = Synthesizer(_FakeProvider("NO_CHANGES"), "m", store, min_new_entries=1)
    for i in range(40):
        synth._log_pass("synthesis", [f"skill_{i}"])
    path = tmp_path / ".synthesis-log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 5
    # The most recent passes are the ones that survive.
    assert json.loads(lines[-1])["changed"] == ["skill_39"]


def test_skill_name_rejects_path_segments() -> None:
    # referenced_skills once rebound the name regex to an unanchored pattern,
    # which let names with separators through validate_skill.
    assert "invalid skill name" in validate_skill("a/b", _GREET)
    assert "invalid skill name" in validate_skill("x/../../pwned", _GREET)
    assert "invalid skill name" in validate_skill("greet\n", _GREET)
    assert "invalid skill name" in validate_skill("a" * 65, _GREET)
    assert validate_skill("greet", _GREET) is None
    # The loose identifier pattern still drives reference extraction.
    assert referenced_skills("skills.greet('x')") == ["greet"]


def test_store_refuses_to_write_outside_root(tmp_path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    for bad in ("x/../../pwned", "a/b", "..", "pwned.py"):
        with pytest.raises(ValueError):
            store.save(bad, _GREET)
        with pytest.raises(ValueError):
            store._path(bad)
        assert store.get(bad) is None
        assert store.delete(bad) is False
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".py"]
    assert written == []
    assert store.list() == []



# ─── Batch 7: one learner per store, bounded inputs, visible spend ───────────


def test_journal_context_stamps_session_and_agent(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.journal_context(session_id="ses_1", agent="worker")
    store.journal_append({"kind": "user", "text": "hi"})
    store.journal_append({"kind": "tool", "name": "Read", "session": "ses_other"})
    first, second = store.journal_entries(0)
    assert first["session"] == "ses_1" and first["agent"] == "worker"
    assert second["session"] == "ses_other" and second["agent"] == "worker"
    store.journal_context()
    store.journal_append({"kind": "user", "text": "untagged"})
    assert "session" not in store.journal_entries(2)[0]


async def test_prompt_separates_sessions_and_budgets_skill_sources(tmp_path, monkeypatch) -> None:
    """CL-3 and CL-7: the journal is rendered one session at a time, and the
    library is no longer re-sent whole every pass: skills the slice used are
    shown in full, the rest until the source budget is spent, then as an
    index line."""
    import fluid.codemode.continual as continual

    store = SkillStore(tmp_path)
    big = '"""Big skill."""\n' + "x = 1\n" * 3000
    store.save("big_one", big)
    store.save("big_two", big)
    store.save("tiny", '"""Tiny."""\n\ndef tiny():\n    return 1\n')
    monkeypatch.setattr(continual, "SKILL_SOURCE_BUDGET", 20_000)
    entries = [
        {"kind": "user", "text": "first", "session": "s1"},
        {"kind": "cell", "code": "from skills import big_two\nbig_two", "session": "s1"},
        {"kind": "user", "text": "second", "session": "s2", "agent": "worker"},
        {"kind": "assistant", "text": "done", "session": "s2", "agent": "worker"},
    ]
    prompt = Synthesizer(provider=None, model="m", store=store)._prompt(entries)
    assert "--- session s1 ---" in prompt
    assert prompt.count("--- session s2, agent worker ---") == 1
    library = prompt.split("## Current memory")[0]
    assert library.count("```python") == 2  # big_two (referenced) and tiny
    assert "### skills.big_one  (used 0x): Big skill. (source not shown this pass" in prompt
    assert prompt.index("### skills.big_two") < prompt.index("### skills.big_one")


async def test_synthesis_is_single_flight_across_processes(tmp_path) -> None:
    """CL-3: a pass holds the store's synthesis lock; a second learner over
    the same store (another process, or a worker) skips the slice instead of
    paying for it again and writing duplicate memories."""
    from fluid.codemode.continual import SYNTHESIS_LOCK
    from fluid.codemode.skills import journal_lock

    store = SkillStore(tmp_path)
    _journal(store, 3)
    provider = _FakeProvider("NO_CHANGES")
    synth = Synthesizer(provider, "test-model", store, min_new_entries=1)
    with journal_lock(tmp_path / SYNTHESIS_LOCK) as held:
        assert held
        assert await synth.run() == []
    assert provider.requests == [] and store.synth_cursor() == 0
    await synth.run()
    assert len(provider.requests) == 1 and store.synth_cursor() == 3


class _UsageProvider(_FakeProvider):
    async def stream(self, request):
        self.requests.append(request)
        yield StreamChunk(
            kind="message_start",
            payload={"usage": {"input_tokens": 120, "output_tokens": 1}, "message": {}},
        )
        yield StreamChunk(kind="text_delta", payload={"text": self._text})
        yield StreamChunk(
            kind="usage",
            payload={"usage": {"output_tokens": 30, "cache_read_input_tokens": 40}},
        )
        yield StreamChunk(kind="message_stop")


async def test_synthesis_spend_is_recorded(tmp_path) -> None:
    """CL-8: the pass's tokens land on the synthesizer and in the pass log."""
    store = SkillStore(tmp_path)
    _journal(store, 3)
    synth = Synthesizer(_UsageProvider("NO_CHANGES"), "m", store, min_new_entries=1)
    await synth.run()
    assert synth.last_pass_usage == {
        "input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 40,
    }
    _journal(store, 2)
    await synth.run()
    assert synth.usage["input_tokens"] == 240 and synth.usage["output_tokens"] == 60
    log = (tmp_path / ".synthesis-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["usage"]["input_tokens"] == 120


def test_live_skill_cap_archives_the_least_used(tmp_path, monkeypatch) -> None:
    """CL-7: the live library is bounded; past the cap the least used, least
    recently active skill moves to the archive."""
    import fluid.codemode.skills as skills_mod

    monkeypatch.setattr(skills_mod, "MAX_LIVE_SKILLS", 3)
    store = SkillStore(tmp_path)
    for i in range(3):
        store.save(f"s{i}", f'"""S{i}."""\n')
    store.note_uses(["s0", "s0", "s1"])
    store.save("s3", '"""S3."""\n')
    assert {s.name for s in store.list()} == {"s0", "s1", "s3"}
    assert (store.archive_dir / "s2.py").is_file()
    assert store.archived_skills()[0].name == "s2"


def test_oversized_skill_file_is_flagged_not_mounted(tmp_path) -> None:
    store = SkillStore(tmp_path)
    (tmp_path / "huge.py").write_text('"""Huge."""\n' + "#" * 70_000, encoding="utf-8")
    skill = store.get("huge")
    assert skill is not None and skill.broken and "byte cap" in skill.broken
    assert "huge" not in store.sources()
