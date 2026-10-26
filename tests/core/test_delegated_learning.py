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
from app.core import credential_gate
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


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: [sys.executable, str(FAKE)])
    monkeypatch.setattr(settings, "fluid_continual", True)
    monkeypatch.setattr(settings, "fluid_continual_model", None)
    # No database: session_memory resolves the installation owner's store.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
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
    """Records which provider binding the adapter built the learner client for."""

    def __init__(self, monkeypatch, reply=None) -> None:
        self.client = FakeInference(reply or _reply("NO_CHANGES"))
        self.built: list[tuple[str, dict]] = []

        async def build(kind, options):
            self.built.append((kind, dict(options.extra), options.model))
            return self.client, None

        monkeypatch.setattr(ha, "_build_provider_client", build)


def _offer_subscription(monkeypatch, row: dict | None = SUBSCRIPTION_ROW) -> list:
    calls: list = []

    def pick(prefer_model=None):
        calls.append(prefer_model)
        if row is None:
            return None
        ids = [m["id"] for m in row["models_json"]]
        return dict(row), prefer_model if prefer_model in ids else row["default_model"]

    monkeypatch.setattr(credential_gate, "subscription_provider_model", pick)
    return calls


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
    _offer_subscription(monkeypatch, None)  # no learner: the journal is still kept
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
    _offer_subscription(monkeypatch)
    run = Run(tmp_path, [[{"op": "text", "text": "hi"}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
        assert run.adapter.engine._journal is None
    assert _journal(_store()) == []


@pytest.mark.asyncio
async def test_an_auxiliary_request_builds_no_journal(monkeypatch, tmp_path):
    _offer_subscription(monkeypatch)
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
async def test_a_finished_reply_runs_a_learner_pass_on_the_owners_subscription(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch, _reply(
        '<memory scope="project">The team deploys with npm test first.</memory>'
    ))
    picks = _offer_subscription(monkeypatch)
    store = _store()
    _seed_backlog(store)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        synthesizer = run.adapter.engine._journal._synthesizer
        assert synthesizer is not None and synthesizer._model == "gpt-sub"
        # Scoped and announced as on the native leg: the project key, the
        # session model's guidance channel, and the engine's own bus.
        assert synthesizer._project == project_key_for(str(tmp_path))
        assert synthesizer._session_model == "opus" and synthesizer._on_change is not None
        assert run.adapter.engine.bus is run.adapter._continual_bus
        # A subscription row's passes get the subscription default budget.
        assert synthesizer._budget() == (200, 8, "subscription_default")
        # The client waits for a pass: nothing was built at connect.
        assert learner.built == []
        await run.turn("Remember we always run npm test before deploying")
        if synthesizer._task is not None:
            await synthesizer._task

    assert picks == [None]
    assert learner.built == [("openai_responses", {
        "provider_id": "row-chatgpt", "provider_type": "chatgpt", "provider_auth_mode": "chatgpt",
    }, "gpt-sub")]
    # A pass reads one session's entries at a time: the earlier backlog, then
    # this session's turn (a due curation pass may follow on the same client).
    prompts = [r.messages[0].content[0].text for r in learner.client.requests]
    assert any("[user] Remember we always run npm test before deploying" in p for p in prompts)
    assert not any("<system-reminder>" in p for p in prompts)
    # Every call was claimed from the store's shared daily budget.
    assert store._facts.inference_budget()["calls"] == len(learner.client.requests)
    assert any("npm test first" in m.text for m in store.memories())


@pytest.mark.asyncio
async def test_a_spent_daily_budget_stops_delegated_passes_too(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch)
    _offer_subscription(monkeypatch)
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
    assert learner.client.requests == []


@pytest.mark.asyncio
async def test_no_usable_learner_provider_keeps_the_journal_for_a_later_pass(monkeypatch, tmp_path):
    learner = _Learner(monkeypatch)
    _offer_subscription(monkeypatch, None)
    store = _store()
    _seed_backlog(store)
    run = Run(tmp_path, [[{"op": "text", "text": "Done."}, {"op": "result"}]])
    async with run:
        await run.turn("hello")
    assert learner.built == [] and learner.client.requests == []
    assert store.synth_cursor() == 0
    assert [e["kind"] for e in _journal(store)][-2:] == ["user", "assistant"]


def test_learner_target_prefers_the_sessions_own_api_row(monkeypatch):
    monkeypatch.setattr(credential_gate, "subscription_provider_model",
                        lambda prefer_model=None: pytest.fail("must not look further"))
    options = SessionOptions(model="opus", extra={
        "provider_id": "row-key", "provider_type": "anthropic", "provider_auth_mode": "api_key",
    })
    target = ha._delegated_learner_target(options, "local")
    assert (target.kind, target.source) == ("anthropic", "session")
    assert target.options.extra["provider_id"] == "row-key"
    assert target.model == ha._native_api_model("opus", options)

    monkeypatch.setattr(settings, "fluid_continual_model", "claude-pinned")
    assert ha._delegated_learner_target(options, "local").model == "claude-pinned"


def test_learner_target_uses_the_pinned_model_on_a_subscription_row(monkeypatch):
    picks = _offer_subscription(monkeypatch)
    monkeypatch.setattr(settings, "fluid_continual_model", "gpt-sub-mini")
    target = ha._delegated_learner_target(SessionOptions(model="opus"), "local")
    assert picks == ["gpt-sub-mini"]
    assert (target.kind, target.model, target.source) == ("openai_responses", "gpt-sub-mini", "subscription")


def test_an_api_users_session_never_spends_the_owners_subscription(monkeypatch):
    picks = _offer_subscription(monkeypatch)
    assert ha._delegated_learner_target(SessionOptions(model="opus"), "api:u1") is None
    assert picks == []


def test_a_claude_subscription_binding_is_not_a_learner_client(monkeypatch):
    """A Claude sign-in row runs only through the CLI; the learner falls
    through to the owner's flat-rate rows."""
    _offer_subscription(monkeypatch)
    options = SessionOptions(model="opus", extra={
        "provider_id": "row-claude", "provider_type": "anthropic", "provider_auth_mode": "oauth",
    })
    target = ha._delegated_learner_target(options, "local")
    assert target.source == "subscription" and target.options.extra["provider_id"] == "row-chatgpt"


def test_subscription_provider_model_never_picks_an_api_key_row(monkeypatch):
    rows = [
        {"id": "key", "provider_type": "openai", "auth_mode": "api_key",
         "models_json": [{"id": "gpt-key"}], "default_model": "gpt-key"},
        dict(SUBSCRIPTION_ROW),
    ]
    monkeypatch.setattr(credential_gate, "usable_provider_rows", lambda: rows)
    row, model = credential_gate.subscription_provider_model()
    assert (row["id"], model) == ("row-chatgpt", "gpt-sub")
    assert credential_gate.subscription_provider_model("gpt-sub-mini")[1] == "gpt-sub-mini"
    monkeypatch.setattr(credential_gate, "usable_provider_rows", lambda: rows[:1])
    assert credential_gate.subscription_provider_model() is None


@pytest.mark.asyncio
async def test_the_learner_client_is_closed_with_the_session(monkeypatch, tmp_path):
    closed: list = []

    class Http:
        async def aclose(self):
            closed.append(True)

    async def build(kind, options):
        return FakeInference(_reply("NO_CHANGES")), Http()

    monkeypatch.setattr(ha, "_build_provider_client", build)
    _offer_subscription(monkeypatch)
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
    # No provider row at all (a local or unknown provider): metered.
    synth, _ = ha.HarnessAdapter._learner_synthesizer(
        owner, FakeInference(_reply("NO_CHANGES")), "m", store, None, session, "s",
        session_model="m", context_window=None)
    assert synth._budget().source == "metered_default"
