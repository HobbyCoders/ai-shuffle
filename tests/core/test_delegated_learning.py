"""Claude Code (delegated) sessions feed the memory learner like native ones (#687).

The real ``HarnessAdapter`` -> ``DelegatedLoop`` -> stream-json pipe runs
against the scripted fake ``claude`` (``fluid/tests/fixtures/
fake_claude_script.py``); the learner's provider is a fake inference client.
Hermetic: no network, no real CLI, no database (the owner store is "local").
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fluid.codemode import continual as continual_mod
from fluid.codemode.continual import low_value_entry
from fluid.codemode.journal import ToolJournal
from fluid.codemode.skills import SkillStore, project_key_for
from fluid.providers.base import StreamChunk
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core import fluid_memory
from app.core.config import settings
from app.core.provider.events import ResultMessage
from app.core.provider.types import SessionOptions

FAKE = (
    Path(__file__).resolve().parents[2]
    / "fluid" / "tests" / "fixtures" / "fake_claude_script.py"
)
TURN_BUDGET = 20.0
APP_SESSION = "app-claude-1"
SUBSCRIPTION_ROW = {
    "id": "row-chatgpt", "provider_type": "chatgpt", "auth_mode": "chatgpt",
    "models_json": [{"id": "gpt-sub"}, {"id": "gpt-sub-mini"}], "default_model": "gpt-sub",
}
#: The owner's Text Generation binding on the ChatGPT subscription row.
CHATGPT_TEXT_GENERATION = {"provider": "row-chatgpt", "model": "gpt-sub"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("FLUID_GUIDANCE_HOME", str(home))
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: [sys.executable, str(FAKE)])
    monkeypatch.setattr(settings, "fluid_continual", True)
    monkeypatch.setattr(settings, "fluid_continual_model", None)
    # No database, so no session row: these tests run as the installation owner.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda _session_id, cwd=None: (
        fluid_memory.store_for_owner("local"), project_key_for(cwd)))
    # Pacing is the daily spread of calls; these tests are about the trigger.
    monkeypatch.setattr(continual_mod, "MIN_PASS_INTERVAL", 0.0)
    monkeypatch.setattr(continual_mod, "PACE_BURST_CALLS", 48)


def _reply(text: str):
    def chunks(request):
        yield StreamChunk("message_start", {"usage": {"input_tokens": 900}})
        yield StreamChunk("text_delta", {"text": text})
        yield StreamChunk("usage", {"usage": {"output_tokens": 30}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})
    return chunks


class FakeInference:
    def __init__(self, reply) -> None:
        self.reply = reply
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.reply(request):
            yield chunk


class _Learner:
    """Records the learner's model calls: the native client the adapter built
    (and its binding), and the generation-helper calls a Claude Code
    session's passes make (``helper``: prompt, model, effort)."""

    def __init__(self, monkeypatch, reply=None) -> None:
        from app.api import generation

        self.client = FakeInference(reply or _reply("NO_CHANGES"))
        self.built: list[tuple[str, dict]] = []
        self.helper: list[tuple[str, Any, Any]] = []
        text = "".join(
            c.payload["text"] for c in (reply or _reply("NO_CHANGES"))(None) if c.kind == "text_delta"
        )

        async def build(kind, options):
            self.built.append((kind, dict(options.extra), options.model))
            return self.client, None

        async def generate(prompt, model=None, timeout_seconds=None, effort=None):
            self.helper.append((prompt, model, effort))
            return text

        monkeypatch.setattr(ha, "_build_provider_client", build)
        monkeypatch.setattr(generation, "generate_with_claude", generate)


def _shapes(monkeypatch, rows: dict | None = None) -> None:
    """``rows`` maps provider ids to their (type, auth mode, dialect) shape."""
    shapes = {"row-chatgpt": ("chatgpt", "chatgpt", None), **(rows or {})}
    monkeypatch.setattr(ha, "_lookup_provider_row_shape", lambda pid: shapes.get(pid, (None, None, None)))


def _text_generation_unused(monkeypatch) -> list:
    """Bind a Text Generation model the learner must never read."""
    from app.core import generation_options

    reads: list = []

    def read():
        reads.append(True)
        return dict(CHATGPT_TEXT_GENERATION)

    monkeypatch.setattr(generation_options, "text_generation_config", read)
    return reads


def _no_learner(monkeypatch) -> None:
    monkeypatch.setattr(ha, "_learner_target", lambda options, owner: None)


def _claude_billing(monkeypatch, mode: str | None) -> None:
    from app.core import usage_attribution

    monkeypatch.setattr(usage_attribution, "claude_billing", lambda: mode)


def _store() -> SkillStore:
    return SkillStore(Path(settings.fluid_skills_dir))


def _journal(store: SkillStore) -> list[dict]:
    if not store._journal_path.exists():
        return []
    return [json.loads(line) for line in store._journal_path.read_text().splitlines() if line]


def _write_skill(home: Path) -> Path:
    skill = home / ".fluid" / "skills" / "deploy-notes" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: deploy-notes\ndescription: How we deploy.\n---\nSteps.\n")
    return skill


def _learning_turn(cwd: Path, skill: Path) -> list[dict]:
    """A Claude Code turn with CLI-owned tools: a test run, a repository read,
    a read of a missing file (the CLI's own wording), a skill read and a
    long command output."""
    steps: list[dict] = []
    for tid, name, args, out, err in (
        ("t1", "Bash", {"command": "npm test"}, "Tests: 3 passed", False),
        ("t2", "Read", {"file_path": str(cwd / "src" / "a.py")}, "1\tx = 1", False),
        ("t3", "Read", {"file_path": str(cwd / "missing.py")},
         "<tool_use_error>File does not exist. Note: your current working directory is "
         f"{cwd}.</tool_use_error>", True),
        ("t4", "Read", {"file_path": str(skill)}, "1\t---\n2\tname: deploy-notes", False),
        ("t5", "Bash", {"command": "cat big.log"}, "line " * 400, False),
    ):
        steps += [
            {"op": "tool_use", "id": tid, "name": name, "input": args},
            {"op": "tool_result", "tool_use_id": tid, "content": out, "is_error": err},
        ]
    return [*steps, {"op": "text", "text": "Tests pass; I deployed per the notes."}, {"op": "result"}]


class Run:
    def __init__(self, tmp_path: Path, turns: list, **extra: Any) -> None:
        script = tmp_path / "script.json"
        script.write_text(json.dumps({"turns": turns}), encoding="utf-8")
        self.options = SessionOptions(
            model="opus",
            system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
            env={"FAKE_CLAUDE_SCRIPT": str(script)},
            extra={"fluid_registry": ToolRegistry(), "app_session_id": APP_SESSION, **extra},
        )
        self.adapter = ha.HarnessAdapter()

    async def __aenter__(self) -> "Run":
        await self.adapter.connect(self.options)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.adapter.disconnect()

    async def turn(self, prompt: str) -> list:
        async def collect() -> list:
            seen: list = []
            await self.adapter.send_query(prompt)
            async for message in self.adapter.receive_events():
                seen.append(message)
                if isinstance(message, ResultMessage):
                    return seen
            raise AssertionError("turn ended without a result")

        return await asyncio.wait_for(collect(), TURN_BUDGET)


# ─── the journal, through the real producing path ────────────────────────────


@pytest.mark.asyncio
async def test_a_claude_turn_journals_user_tool_friction_and_assistant_records(monkeypatch, tmp_path):
    _no_learner(monkeypatch)  # no learner: the journal is still kept
    skill = _write_skill(tmp_path / "home")
    run = Run(tmp_path, [_learning_turn(tmp_path, skill)])
    async with run:
        await run.turn("Deploy it the usual way")
        engine = run.adapter.engine
        assert isinstance(engine._journal, ToolJournal)
        assert engine._journal._synthesizer is None
        # The skill-load count runs off the turn; let it land.
        await asyncio.gather(*engine._skill_read_tasks)

    entries = _journal(_store())
    kinds = [e["kind"] for e in entries]
    assert kinds == ["user", "tool", "tool", "tool", "friction", "tool", "tool", "assistant"]
    # Every record is stamped like a native one: app session and project key.
    project = project_key_for(str(tmp_path))
    assert {(e["session"], e["project"]) for e in entries} == {(APP_SESSION, project)}

    user = entries[0]
    # The app's turn note (date, memory) rides the prompt as reminders; the
    # learner sees only what the user typed.
    assert user["text"] == "Deploy it the usual way"
    tools = [e for e in entries if e["kind"] == "tool"]
    assert [t["name"] for t in tools] == ["Bash", "Read", "Read", "Read", "Bash"]
    assert json.loads(tools[0]["input"]) == {"command": "npm test"}
    assert tools[0]["output"] == "Tests: 3 passed" and "error" not in tools[0]
    assert tools[2]["error"] is True and "File does not exist" in tools[2]["output"]
    # Size clipping is the native journal's: 400 chars of output.
    assert len(tools[4]["output"]) == 400
    friction = entries[4]
    assert (friction["signal"], friction["name"]) == ("path_not_found", "Read")
    assert entries[-1]["text"] == "Tests pass; I deployed per the notes."

    # The same low-value filter: a successful in-repo read is consumed
    # unshown; a failed read and a command are shown to the learner.
    assert low_value_entry(tools[1]) is True
    assert low_value_entry(tools[2]) is False
    assert low_value_entry(tools[0]) is False

    # A Read of a listed SKILL.md counts as a load, as the Skill tool does.
    usage = json.loads((tmp_path / "home" / ".fluid" / "skills" / ".usage.json").read_text())
    assert usage[str(skill)]["loads"] == 1


@pytest.mark.asyncio
async def test_learning_off_builds_no_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "fluid_continual", False)
    run = Run(tmp_path, [[{"op": "text", "text": "hi"}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
        assert run.adapter.engine._journal is None
    assert _journal(_store()) == []


@pytest.mark.asyncio
async def test_an_auxiliary_request_builds_no_journal(monkeypatch, tmp_path):
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="opus", extra={"assistant_proactive": {"kind": "x"}})
    assert await adapter._delegated_journal(adapter._options, "ses_x") == (None, None)


# ─── the learner pass and its provider ───────────────────────────────────────


def _seed_backlog(store: SkillStore, n: int = 24) -> None:
    """Entries an earlier session left unconsumed (one with no learner)."""
    store.journal_context(session_id="app-earlier", project="project:p0")
    for i in range(n):
        store.journal_append({"kind": "tool", "name": "Bash",
                              "input": json.dumps({"command": f"make step{i}"}),
                              "output": f"step {i} ok"})


@pytest.mark.asyncio
async def test_a_finished_reply_runs_a_learner_pass_on_the_sessions_model_and_effort(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch, _reply(
        '<memory scope="project">The team deploys with npm test first.</memory>'
    ))
    reads = _text_generation_unused(monkeypatch)
    _claude_billing(monkeypatch, "subscription")
    store = _store()
    _seed_backlog(store)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    run.options.effort = "high"
    async with run:
        synthesizer = run.adapter.engine._journal._synthesizer
        assert synthesizer is not None and synthesizer._model == "opus"
        # Scoped and announced as on the native leg: the project key, the
        # session model's guidance channel, and the engine's own bus.
        assert synthesizer._project == project_key_for(str(tmp_path))
        assert synthesizer._session_model == "opus" and synthesizer._on_change is not None
        assert run.adapter.engine.bus is run.adapter._continual_bus
        # The CLI on a Claude subscription: the subscription default budget.
        assert synthesizer._budget() == (200, 8, "subscription_default")
        # The owner raises the chat's effort mid-chat (set_run_settings
        # writes the session options): the next pass follows it.
        run.options.effort = "max"
        await run.turn("Remember we always run npm test before deploying")
        if synthesizer._task is not None:
            await synthesizer._task

    # Never the Text Generation model, and no native client: the CLI session's
    # passes run through the generation helper on the chat's own binding.
    assert reads == [] and learner.built == []
    assert learner.helper and {(m, e) for _, m, e in learner.helper} == {("opus", "max")}
    # A pass reads one session's entries at a time: the earlier backlog, then
    # this session's turn (a due curation pass may follow).
    prompts = [p for p, _, _ in learner.helper]
    # The line names the family the user was talking to (the session's opus).
    assert any("[user to claude] Remember we always run npm test before deploying" in p for p in prompts)
    assert not any("<system-reminder>" in p for p in prompts)
    # Every call was claimed from the store's shared daily budget.
    assert store._facts.inference_budget()["calls"] == len(learner.helper)
    assert any("npm test first" in m.text for m in store.memories())


@pytest.mark.asyncio
async def test_a_spent_daily_budget_stops_delegated_passes_too(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch)
    _claude_billing(monkeypatch, "subscription")
    store = _store()
    _seed_backlog(store)
    # Other sessions on this store spent the day's synthesis share, at the
    # subscription learner's own (larger) daily budget.
    budget = continual_mod.learner_budget(None, "subscription")
    while store._facts.claim_inference_call(reserve=budget.reserved, limit=budget.daily):
        pass
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
    assert learner.client.requests == [] and learner.helper == []


@pytest.mark.asyncio
async def test_no_usable_learner_provider_keeps_the_journal_for_a_later_pass(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch)
    _no_learner(monkeypatch)
    store = _store()
    _seed_backlog(store)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
    assert learner.built == [] and learner.client.requests == [] and learner.helper == []
    assert store.synth_cursor() == 0
    assert [e["kind"] for e in _journal(store)][-2:] == ["user", "assistant"]


def test_the_owners_learner_runs_on_the_sessions_native_row_model_and_effort(monkeypatch):
    """A session on a row a native client can call: the pass calls that row
    with the chat's own model, sized for its window, at the chat's current
    effort. Never the Text Generation binding."""
    reads = _text_generation_unused(monkeypatch)
    monkeypatch.setattr(ha, "_native_context_window", lambda kind, options: 272_000)
    options = SessionOptions(model="opus", effort="high", extra={
        "provider_id": "row-key", "provider_type": "anthropic", "provider_auth_mode": "api_key",
    })
    target = ha._learner_target(options, "local")
    assert (target.kind, target.model) == ("anthropic", ha._native_api_model("opus", options))
    assert target.options.extra == {
        "provider_id": "row-key", "provider_type": "anthropic", "provider_auth_mode": "api_key",
    }
    assert target.context_window == 272_000
    assert target.effort() == "high"
    options.effort = "low"  # changed mid-chat
    assert target.effort() == "low"
    options.effort = "bogus"  # never forwarded to a provider
    assert target.effort() is None
    assert reads == []

    # A pinned learner model keeps the learner's own low effort.
    monkeypatch.setattr(settings, "fluid_continual_model", "gpt-sub-mini")
    target = ha._learner_target(options, "local")
    assert (target.model, target.effort) == ("gpt-sub-mini", continual_mod.SYNTH_EFFORT)


def test_any_other_binding_runs_through_the_generation_helper_on_the_sessions_binding(monkeypatch):
    """A Claude sign-in row (CLI only), native Anthropic with no row, or any
    row without a native client: the pass goes through the generation helper
    on the chat's own model and row, so learning works on every provider.
    The row's shape is kept for billing."""
    reads = _text_generation_unused(monkeypatch)
    _shapes(monkeypatch, {"row-claude": ("anthropic", "oauth", None)})
    target = ha._learner_target(SessionOptions(model="opus", extra={"provider_id": "row-claude"}), "local")
    assert (target.kind, target.model) == (ha.GENERATION_HELPER_LEARNER, "opus")
    assert target.options.extra == {"provider_id": "row-claude", "provider_type": "anthropic",
                                    "provider_auth_mode": "oauth"}

    target = ha._learner_target(SessionOptions(model="sonnet"), "local")
    assert (target.kind, target.model) == (ha.GENERATION_HELPER_LEARNER, "sonnet")
    assert target.options.extra == {}
    assert reads == []


def test_a_session_without_a_model_has_no_learner(monkeypatch):
    _shapes(monkeypatch)
    assert ha._learner_target(SessionOptions(model=""), "local") is None


def test_an_api_users_session_never_spends_the_owners_binding(monkeypatch):
    reads = _text_generation_unused(monkeypatch)
    _shapes(monkeypatch)
    # A Claude Code session with no row would run on the owner's CLI login.
    assert ha._learner_target(SessionOptions(model="opus"), "api:u1") is None
    assert reads == []
    # Its own API-key row, when a native client can call it.
    own = SessionOptions(model="opus", extra={
        "provider_id": "row-key", "provider_type": "anthropic", "provider_auth_mode": "api_key",
    })
    target = ha._learner_target(own, "api:u1")
    assert target.kind == "anthropic"
    assert target.model == ha._native_api_model("opus", own)


@pytest.mark.asyncio
async def test_the_generation_helper_learner_answers_as_stream_chunks(monkeypatch):
    from types import SimpleNamespace

    from app.api import generation

    seen: list = []

    async def generate(prompt, model=None, timeout_seconds=None, effort=None):
        seen.append((prompt, model, timeout_seconds, effort))
        return "NO_CHANGES"

    monkeypatch.setattr(generation, "generate_with_claude", generate)
    block = SimpleNamespace(text="[user] journal excerpt")
    request = SimpleNamespace(system="SYSTEM RULES", messages=[SimpleNamespace(content=[block])],
                              effort="xhigh")
    provider = ha._LearnerProvider(ha.GENERATION_HELPER_LEARNER,
                                   SessionOptions(model="m", extra={"provider_id": "row-x"}))
    chunks = [c async for c in provider.stream(request)]
    assert [c.kind for c in chunks] == ["text_delta", "message_stop"]
    assert chunks[0].payload["text"] == "NO_CHANGES"
    prompt, model, timeout, effort = seen[0]
    assert "SYSTEM RULES" in prompt and "[user] journal excerpt" in prompt
    # The session's row and model as one run binding, at the pass's effort.
    assert (model, timeout, effort) == ("prov:row-x:m", ha.GENERATION_HELPER_LEARNER_TIMEOUT, "xhigh")

    # No row (the Claude Code CLI): the bare model; no effort, the model's default.
    request.effort = None
    seen.clear()
    provider = ha._LearnerProvider(ha.GENERATION_HELPER_LEARNER, SessionOptions(model="opus"))
    [c async for c in provider.stream(request)]
    assert (seen[0][1], seen[0][3]) == ("opus", None)

    async def broken(prompt, timeout_seconds=None, **_):
        raise RuntimeError("provider down")

    monkeypatch.setattr(generation, "generate_with_claude", broken)
    chunks = [c async for c in provider.stream(request)]
    assert [c.kind for c in chunks] == ["error"] and "provider down" in chunks[0].payload["message"]


@pytest.mark.asyncio
async def test_a_switched_off_learner_makes_no_calls(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch)
    monkeypatch.setattr(fluid_memory, "learner_enabled", lambda: False)
    store = _store()
    _seed_backlog(store)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
        synthesizer = run.adapter.engine._journal._synthesizer
        if synthesizer is not None and synthesizer._task is not None:
            await synthesizer._task
    assert learner.client.requests == [] and learner.built == [] and learner.helper == []
    assert store._facts.inference_budget().get("calls", 0) == 0


@pytest.mark.asyncio
async def test_the_learner_client_is_closed_with_the_session(monkeypatch, tmp_path):
    closed: list = []

    class Http:
        async def aclose(self):
            closed.append(True)

    async def build(kind, options):
        return FakeInference(_reply("NO_CHANGES")), Http()

    monkeypatch.setattr(ha, "_build_provider_client", build)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        learner = run.adapter._learner_provider
        assert learner is not None
        await learner._ensure()
    assert closed == [True] and run.adapter._learner_provider is None


def test_a_learner_on_an_api_key_row_keeps_the_metered_budget(monkeypatch):
    from types import SimpleNamespace

    store = _store()
    session = SessionOptions(model="opus", cwd=".", extra={})
    api_row = SessionOptions(model="claude", extra={
        "provider_id": "row-key", "provider_type": "anthropic", "provider_auth_mode": "api_key",
    })
    owner = SimpleNamespace(_announce_continual=None)
    synth, _ = ha.HarnessAdapter._learner_synthesizer(
        owner, FakeInference(_reply("NO_CHANGES")), "claude", store, None, session, "s",
        session_model="opus", context_window=None, binding=api_row)
    assert synth._budget() == (48, 2, "metered_default")
    # A row given by id only is looked up for its shape.
    monkeypatch.setattr(ha, "_lookup_provider_row_shape", lambda pid: ("grok", "grok", None))
    synth, _ = ha.HarnessAdapter._learner_synthesizer(
        owner, FakeInference(_reply("NO_CHANGES")), "grok-x", store, None,
        SessionOptions(model="grok-x", cwd=".", extra={"provider_id": "row-grok"}), "s",
        session_model="grok-x", context_window=None)
    assert synth._budget().source == "subscription_default"
    # No provider row: the Claude Code CLI leg, billed as Claude's auth mode
    # is; unreadable is unknown, capped as metered.
    from app.core import usage_attribution
    for mode, source in (("api", "metered_default"), (None, "metered_default"),
                         ("subscription", "subscription_default")):
        monkeypatch.setattr(usage_attribution, "claude_billing", lambda mode=mode: mode)
        synth, _ = ha.HarnessAdapter._learner_synthesizer(
            owner, FakeInference(_reply("NO_CHANGES")), "m", store, None, session, "s",
            session_model="m", context_window=None)
        assert synth._budget().source == source, mode


def test_a_generation_helper_learner_bills_by_the_sessions_row(monkeypatch):
    """Not by the Text Generation binding: a chat on a Claude plan row is
    flat-rate even when Text Generation sits on a metered API key."""
    from types import SimpleNamespace

    from app.core import generation_options

    monkeypatch.setattr(generation_options, "text_generation_config",
                        lambda: {"model": "claude", "provider": "row-key"})
    shapes = {"row-plan": ("anthropic", "oauth", None), "row-key": ("anthropic", "api_key", None)}
    monkeypatch.setattr(ha, "_lookup_provider_row_shape", lambda pid: shapes[pid])
    binding = SessionOptions(model="opus", extra={"provider_id": "row-plan"})
    provider = ha._LearnerProvider(ha.GENERATION_HELPER_LEARNER, binding)
    owner = SimpleNamespace(_announce_continual=None)
    synth, _ = ha.HarnessAdapter._learner_synthesizer(
        owner, provider, "opus", _store(), None, SessionOptions(model="opus", cwd=".", extra={}),
        "s", session_model="opus", context_window=None, binding=binding)
    assert synth._budget(refresh=True).source == "subscription_default"


@pytest.mark.asyncio
async def test_a_mid_chat_model_switch_moves_the_learner_to_the_new_model(monkeypatch):
    """set_model hot-swaps without a reconnect; the learner follows, so a pass
    never pairs the old model with an effort picked for the new one."""
    _shapes(monkeypatch)
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="opus", cwd=".", extra={})
    target = ha._learner_target(adapter._options, "local")
    provider = ha._LearnerProvider(target.kind, target.options)
    adapter._learner_provider = provider
    synth, _ = adapter._learner_synthesizer(
        provider, target.model, _store(), None, adapter._options, "s",
        session_model="opus", context_window=None, binding=target.options, effort=target.effort)
    assert adapter._learner_synth is synth and synth._model == "opus"

    adapter._options.model = "sonnet"  # what set_model writes
    await adapter._retarget_learner()
    assert synth._model == "sonnet" and provider._options.model == "sonnet"

    # A pinned learner model stays where it is.
    monkeypatch.setattr(settings, "fluid_continual_model", "pinned-model")
    adapter._options.model = "haiku"
    await adapter._retarget_learner()
    assert synth._model == "sonnet"
