"""Continual harness v2 — verification, episodic journal, checkpointing.

The three "not yet" items from docs/agent-experience.md made real:

1. **Skill verification** — import + ``_selftest()`` in a throwaway no-tools
   kernel; on save (rejected if failing) and on mount (broken skills excluded
   and flagged); verdicts cached by mtime.
2. **Episodic memory** — user/assistant conversation events journaled next to
   cells and rendered into the synthesis prompt.
3. **Namespace checkpointing** — kernel death (timeout/OOM) restores picklable
   variables from the last post-exec checkpoint; the error message reports
   what survived.
"""

import pytest

from fluid.codemode import (
    HostBridge,
    KernelConfig,
    Projection,
    SkillStore,
    SubprocessKernel,
    Synthesizer,
    verify_sources,
    verify_store,
)
from fluid.codemode.kernel import HostReply
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import StreamChunk
from fluid.tools.registry import ToolCtx, ToolRegistry


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


_GOOD = (
    '"""Double a number."""\n\n'
    "def double(x):\n    return 2 * x\n\n"
    "def _selftest():\n    assert double(2) == 4\n"
)
_BAD_SELFTEST = (
    '"""Broken doubler."""\n\n'
    "def bad(x):\n    return 3 * x\n\n"
    "def _selftest():\n    assert bad(2) == 4\n"
)
_BAD_IMPORT = '"""Won\'t import."""\n\nraise RuntimeError("rotten module")\n'


# ─── checkpointing ───────────────────────────────────────────────────────────


async def test_checkpoint_restores_data_after_timeout() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("data = [1, 2, 3]\ntotal = sum(data)", timeout=5,
                         on_host_call=_no_tools)
        assert r.error is None
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "exceeded" in r.error
        assert "restored from last checkpoint" in r.error
        r = await k.exec("(data, total)", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "([1, 2, 3], 6)"
    finally:
        await k.stop()


async def test_checkpoint_reports_unrestorable_functions() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("def helper():\n    return 1\nx = 42", timeout=5,
                         on_host_call=_no_tools)
        assert r.error is None
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None
        assert "not restorable" in r.error and "helper" in r.error
        r = await k.exec("x", timeout=5, on_host_call=_no_tools)
        assert r.value_repr == "42"
    finally:
        await k.stop()


async def test_checkpoint_disabled_loses_namespace() -> None:
    k = SubprocessKernel(KernelConfig(checkpoint=False))
    await k.start()
    try:
        r = await k.exec("x = 1", timeout=5, on_host_call=_no_tools)
        assert r.error is None
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "namespace lost" in r.error
        r = await k.exec("x", timeout=5, on_host_call=_no_tools)
        assert r.error is not None and "NameError" in r.error
    finally:
        await k.stop()


async def test_no_checkpoint_yet_reports_lost() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        # first exec never completes → nothing was ever checkpointed
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "namespace lost" in r.error
    finally:
        await k.stop()


# ─── skill verification ──────────────────────────────────────────────────────


async def test_verify_sources_pass_and_fail() -> None:
    verdicts = await verify_sources({
        "double": _GOOD,
        "bad": _BAD_SELFTEST,
        "rotten": _BAD_IMPORT,
    })
    assert verdicts["double"] is None
    assert "AssertionError" in verdicts["bad"]
    assert "rotten module" in verdicts["rotten"]


async def test_verify_selftest_cannot_call_tools() -> None:
    source = (
        '"""Impure."""\n'
        "from tools.get_weather import get_weather\n\n"
        "def impure():\n    return get_weather(city='X')\n\n"
        "def _selftest():\n    impure()\n"
    )
    # `tools.get_weather` isn't mounted in the verification kernel at all, so
    # even the import fails — impure skills can't pass verification.
    verdicts = await verify_sources({"impure": source})
    assert verdicts["impure"] is not None


async def test_verify_store_caches_by_mtime(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("double", _GOOD)
    store.save("bad", _BAD_SELFTEST)
    verdicts = await verify_store(store)
    assert verdicts["double"] is None and verdicts["bad"] is not None
    assert store.get("double").broken is None
    assert "AssertionError" in store.get("bad").broken
    # cached: nothing stale → no re-verification
    assert await verify_store(store) == {}
    # editing a skill makes it stale again
    store.save("bad", _GOOD.replace("double", "bad"))
    verdicts = await verify_store(store)
    assert verdicts == {"bad": None}
    assert store.get("bad").broken is None


async def test_broken_skill_excluded_from_mount_and_flagged(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save("double", _GOOD)
    store.save("bad", _BAD_SELFTEST)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)

    class _FakeLoop:
        permissions = None
        hooks = None
        ctx = ToolCtx()

        async def _emit(self, event) -> None:
            pass

    try:
        ok = await session.run_python(
            sid="s", turn_id="t",
            input={"code": "from skills.double import double\ndouble(4)"},
            loop=_FakeLoop(),
        )
        assert not ok.is_error and "=> 8" in ok.text
        broken = await session.run_python(
            sid="s", turn_id="t",
            input={"code": "import skills.bad"},
            loop=_FakeLoop(),
        )
        assert broken.is_error and "No module named 'skills.bad'" in broken.text

        listing = session.render_list()
        assert "BROKEN, not mounted" in listing and "AssertionError" in listing
    finally:
        await session.aclose()


async def test_save_skill_rejects_failing_selftest(tmp_path) -> None:
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
        escaped = _BAD_SELFTEST.replace('"', '\\"').replace("\n", "\\n")
        result = await kernel.exec(
            "from tools.save_skill import save_skill\n"
            f'save_skill(name="bad", source="{escaped}")\n',
            timeout=30, on_host_call=bridge.on_host_call,
        )
        assert result.error is not None and "verification failed" in result.error
        assert store.get("bad") is None

        escaped_good = _GOOD.replace('"', '\\"').replace("\n", "\\n")
        result = await kernel.exec(
            "from tools.save_skill import save_skill\n"
            f'save_skill(name="double", source="{escaped_good}")\n',
            timeout=30, on_host_call=bridge.on_host_call,
        )
        assert result.error is None, result.error
        assert "saved and verified" in result.value_repr
        assert store.get("double") is not None
        assert not store.needs_verification("double")
    finally:
        await kernel.stop()


# ─── episodic journal ────────────────────────────────────────────────────────


class _FakeProvider:
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        yield StreamChunk(kind="text_delta", payload={"text": self._text})
        yield StreamChunk(kind="message_stop")


def test_record_event_journals_conversation(tmp_path) -> None:
    store = SkillStore(tmp_path)
    session = CodeModeSession(ToolRegistry(), skills=store)
    session.record_event("user", "Fetch the weather for Paris and Rome")
    session.record_event("assistant", "Done — both fetched and compared.")
    session.record_event("user", "")  # blank → ignored
    entries = store.journal_entries()
    assert [e["kind"] for e in entries] == ["user", "assistant"]
    assert entries[0]["text"].startswith("Fetch the weather")


def test_record_event_without_store_is_noop() -> None:
    session = CodeModeSession(ToolRegistry())  # no SkillStore
    session.record_event("user", "hello")  # must not raise


async def test_prompt_interleaves_conversation_and_cells(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Compare weather in 2 cities"})
    store.journal_append({"kind": "cell", "code": "w = 1", "host_calls": 2})
    store.journal_append({"kind": "assistant", "text": "Here is the comparison."})
    provider = _FakeProvider("NO_CHANGES")
    synth = Synthesizer(provider, "m", store, min_new_entries=1)
    await synth.run()
    prompt = provider.requests[0].messages[0].content[0].text
    assert "[user] Compare weather in 2 cities" in prompt
    assert "w = 1" in prompt
    assert "[assistant] Here is the comparison." in prompt
    # conversation events count toward synthesis pressure
    assert store.synth_cursor() == 3


# ─── synthesizer verification gate ───────────────────────────────────────────


async def test_synthesizer_rejects_failing_candidate(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "cell", "code": "x = 1"})
    reply = f'<skill name="bad">\n{_BAD_SELFTEST}</skill>'
    synth = Synthesizer(_FakeProvider(reply), "m", store, min_new_entries=1)
    assert await synth.run() == []
    assert synth.last_error is not None and "verification failed" in synth.last_error
    assert store.get("bad") is None


async def test_synthesizer_accepts_and_preverifies(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "cell", "code": "x = 1"})
    reply = f'<skill name="double">\n{_GOOD}</skill>'
    synth = Synthesizer(_FakeProvider(reply), "m", store, min_new_entries=1)
    assert await synth.run() == ["double"]
    assert store.get("double").broken is None
    assert not store.needs_verification("double")  # verified at synthesis time
