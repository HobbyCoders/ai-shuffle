"""Phase 2 pins: guidance proposals, and the gate that disposes of them.

The channel only exists when the app hands fluid's ``Synthesizer`` a
``session_model`` and a ``guidance_snapshot`` — omit either and fluid disables
proposals entirely (its documented safe default for embedding hosts). These
tests pin the wiring at both ends: what the session passes in, and what the
out-of-band gate surface does with what comes back out.

The invariant that matters most is the pairing. A proposal's stem must be a
substring of the session model id or the committed file would never load, and
the snapshot must be the raw text of the very files the prompt carries or an
amendment silently truncates the file it replaces.
"""

import json

import pytest
from fluid.codemode.anchors import ReplayOutcome
from fluid.codemode.evolution import (
    Proposal,
    evolution_log,
    parked_proposals,
    pending_queue,
    queue_pending,
)

from app.core import guidance_surface as gs
from app.core.provider.types import SessionOptions

GLM = "z-ai/glm-5.3-flash"


@pytest.fixture
def install(monkeypatch, tmp_path):
    """An install whose skills root and guidance home are both throwaway."""
    skills = tmp_path / "skills"
    skills.mkdir()
    home = tmp_path / "home"
    (home / ".fluid" / "guidance").mkdir(parents=True)
    monkeypatch.setenv(gs.GUIDANCE_HOME_ENV, str(home))

    import app.core.fluid_memory as fm

    monkeypatch.setattr(fm, "skills_root", lambda: skills)
    return skills, home


def _write_guidance(home, stem: str, content: str) -> None:
    (home / ".fluid" / "guidance" / f"{stem}.md").write_text(
        content, encoding="utf-8"
    )


class ScriptedRunner:
    """Replay outcomes keyed by anchor id; unknown ids pass (fluid's own
    test double — the gate only needs ``async replay(anchor, proposal=)``)."""

    def __init__(self, outcomes=None) -> None:
        self.outcomes = outcomes or {}
        self.replayed: list[str] = []

    async def replay(self, anchor, proposal=None) -> ReplayOutcome:
        self.replayed.append(anchor.id)
        return self.outcomes.get(anchor.id, ReplayOutcome(status="ok"))


def _proposal(**overrides) -> Proposal:
    fields = dict(
        kind="guidance",
        target="glm",
        content="Call tools directly; never describe an intended call.",
        rationale="three journal turns described actions instead of doing them",
        motivation={
            "prompt": "list the files in this project",
            "checks": [{"kind": "tool_called", "name": "Glob"}],
        },
        proposer="synthesizer",
    )
    fields.update(overrides)
    return Proposal(**fields)


# ─── the session end: what the Synthesizer is handed ─────────────────────────


class _CapturingSynthesizer:
    kwargs: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        type(self).kwargs = dict(kwargs)
        self.args = args

    def mark_urgent(self) -> None:
        pass

    def kick(self) -> None:
        pass


class _CtxLoop:
    def __init__(self, **kwargs) -> None:
        from fluid.tools.registry import ToolCtx

        self.ctx = kwargs.get("ctx") or ToolCtx()
        self.agents = list(kwargs.get("agents") or ())

    async def _ensure_session(self):
        class _Meta:
            session_id = "ses_x"

        return _Meta()

    async def aclose(self):
        pass


async def _fake_provider_client(kind, options):
    return object(), None


async def _connect(monkeypatch, tmp_path, model: str):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(ha, "Synthesizer", _CapturingSynthesizer)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    _CapturingSynthesizer.kwargs = {}
    await ha.HarnessAdapter().connect(
        SessionOptions(
            model=model,
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    return _CapturingSynthesizer.kwargs


@pytest.mark.asyncio
async def test_synthesizer_gets_the_session_model_and_its_guidance(
    monkeypatch, tmp_path, install
):
    """Both arguments, or the proposal channel stays shut."""
    _skills, home = install
    _write_guidance(home, "glm", "Verify writes by reading them back.")
    _write_guidance(home, "claude", "not this family")

    kwargs = await _connect(monkeypatch, tmp_path, GLM)

    # The session's id, NOT the resolved api id — a stem has to be a substring
    # of whatever the read path keys on.
    assert kwargs["session_model"] == GLM
    # Only the matching family, raw.
    assert kwargs["guidance_snapshot"] == {
        "glm": "Verify writes by reading them back."
    }


@pytest.mark.asyncio
async def test_snapshot_is_empty_when_no_file_matches(
    monkeypatch, tmp_path, install
):
    """A model with no guidance file still opens the channel — fluid's prompt
    says "no guidance file exists yet", which is how a new family gets its
    first one."""
    _skills, home = install
    _write_guidance(home, "glm", "glm rules")

    kwargs = await _connect(monkeypatch, tmp_path, "claude-opus-5")

    assert kwargs["session_model"] == "claude-opus-5"
    assert kwargs["guidance_snapshot"] == {}


def test_prompt_carries_the_learned_file_after_the_shipped_blocks(
    monkeypatch, tmp_path, install
):
    """The read half. Without it the gate writes files no session ever sees."""
    import app.core.provider.harness_adapter as ha

    _skills, home = install
    _write_guidance(home, "glm", "GLM-ONLY-MARKER: read your writes back.")

    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "BASE", None, SessionOptions(model=GLM, cwd=str(tmp_path)),
    )
    text = compiled.text
    assert "GLM-ONLY-MARKER" in text
    # After the shipped factory blocks, which stay in code.
    shipped = ha._model_guidance_blocks(GLM)
    assert shipped, "glm should match fluid's shipped family tables"
    assert text.index(shipped[0]) < text.index("GLM-ONLY-MARKER")


def test_snapshot_and_prompt_read_the_same_files(install):
    """The pairing invariant: what the synthesizer amends is what the session
    carries. A snapshot of anything else makes every amendment a guess."""
    _skills, home = install
    _write_guidance(home, "glm", "one\ntwo")

    snapshot = gs.session_guidance_snapshot(GLM)
    blocks = gs.prompt_guidance_blocks(GLM)

    assert set(snapshot) == {"glm"}
    assert len(blocks) == 1
    # Same content; the prompt copy is screened/capped, the snapshot is raw.
    assert "one" in blocks[0] and "two" in blocks[0]
    assert snapshot["glm"] == "one\ntwo"


def test_no_model_reads_nothing(install):
    assert gs.session_guidance_snapshot(None) == {}
    assert gs.prompt_guidance_blocks(None) == ()


# ─── the gate end: disposition ───────────────────────────────────────────────


def test_pending_surfaces_what_the_synthesizer_queued(install):
    skills, _home = install
    queue_pending(skills, _proposal())

    records = gs.pending_proposals()

    assert [r["target"] for r in records] == ["glm"]
    assert records[0]["proposer"] == "synthesizer"
    assert records[0]["motivation"]["prompt"] == "list the files in this project"


@pytest.mark.asyncio
async def test_rejection_parks_the_proposal_instead_of_dropping_it(install):
    """Parked records are the evidence a later, better proposal is built from
    — a dismissal that deletes them throws that away."""
    skills, home = install
    queue_pending(skills, _proposal())

    assert await gs.reject_pending("glm", reason="operator: too broad") is True

    assert gs.pending_proposals() == []
    parked = parked_proposals(skills)
    assert len(parked) == 1
    assert parked[0]["stage"] == "validity"
    assert parked[0]["reasons"] == ["operator: too broad"]
    # The motivation survives the round trip, or a requeue parks as unevidenced.
    assert parked[0]["motivation"]["checks"] == [
        {"kind": "tool_called", "name": "Glob"}
    ]
    # Nothing was written to the guidance directory.
    assert not (home / ".fluid" / "guidance" / "glm.md").exists()


@pytest.mark.asyncio
async def test_rejecting_an_absent_proposal_is_a_no_op(install):
    assert await gs.reject_pending("glm") is False


@pytest.mark.asyncio
async def test_a_surviving_proposal_commits_to_the_guidance_directory(install):
    skills, home = install
    queue_pending(skills, _proposal())
    runner = ScriptedRunner()

    verdicts = await gs.evaluate_pending(runner=runner)

    assert [v["committed"] for v in verdicts] == [True]
    # The improvement stage replayed the motivating case.
    assert runner.replayed == ["motivation"]
    written = home / ".fluid" / "guidance" / "glm.md"
    assert written.read_text(encoding="utf-8").startswith("Call tools directly")
    # Durable, and out of the queue.
    assert gs.pending_proposals() == []
    assert [r["target"] for r in evolution_log(skills)] == ["glm"]
    # ...and the next session reads it.
    assert gs.session_guidance_snapshot(GLM)["glm"].startswith("Call tools")


@pytest.mark.asyncio
async def test_an_unevidenced_synthesizer_proposal_is_parked(install):
    """The synthesizer never gets the no-motivation waiver."""
    skills, home = install
    queue_pending(skills, _proposal(motivation=None))

    verdicts = await gs.evaluate_pending(runner=ScriptedRunner())

    assert verdicts[0]["committed"] is False
    assert verdicts[0]["stage"] == "improvement"
    assert not (home / ".fluid" / "guidance" / "glm.md").exists()
    assert gs.pending_proposals() == []
    assert parked_proposals(skills)[0]["stage"] == "improvement"


@pytest.mark.asyncio
async def test_a_regressing_proposal_is_parked_at_retention(install):
    """Retention is the stage that stops a change from rewiring behavior for
    every future session of the family."""
    from fluid.codemode.anchors import AnchorStore

    skills, home = install
    store = AnchorStore(skills)
    for i in range(2):
        anchor = store.add(
            title=f"anchor {i}",
            prompt="what did we decide about the deploy cadence?",
            checks=[{"kind": "contains", "pattern": "weekly"}],
        )
        store.record_result(anchor.id, passed=True)
    queue_pending(skills, _proposal())
    runner = ScriptedRunner(
        {
            "a1": ReplayOutcome(status="ok", failures=("wanted weekly",)),
            "a2": ReplayOutcome(status="ok", failures=("wanted weekly",)),
        }
    )

    verdicts = await gs.evaluate_pending(runner=runner, budget=1)

    assert verdicts[0]["stage"] == "retention"
    assert verdicts[0]["regressions"] == 2
    assert not (home / ".fluid" / "guidance" / "glm.md").exists()


@pytest.mark.asyncio
async def test_one_regression_still_commits_under_the_default_budget(install):
    """HCL's B=1: a moderate budget beats both strict stability and unguarded
    plasticity, so one regression is tolerated by design."""
    from fluid.codemode.anchors import AnchorStore

    skills, home = install
    store = AnchorStore(skills)
    for i in range(2):
        anchor = store.add(title=f"anchor {i}", prompt="p", checks=[
            {"kind": "contains", "pattern": "x"}
        ])
        store.record_result(anchor.id, passed=True)
    queue_pending(skills, _proposal())
    runner = ScriptedRunner(
        {"a1": ReplayOutcome(status="ok", failures=("regressed",))}
    )

    verdicts = await gs.evaluate_pending(runner=runner, budget=1)

    assert verdicts[0]["committed"] is True
    assert verdicts[0]["regressions"] == 1
    assert (home / ".fluid" / "guidance" / "glm.md").exists()


@pytest.mark.asyncio
async def test_an_empty_queue_costs_nothing(install):
    assert await gs.evaluate_pending(runner=ScriptedRunner()) == []


@pytest.mark.asyncio
async def test_gate_reports_when_no_runner_can_be_built(install, monkeypatch):
    """Replays need fluid's provider configuration, which the app does not
    keep. Say so instead of half-running the gate."""
    skills, _home = install
    queue_pending(skills, _proposal())

    def _boom(*args, **kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("fluid.config.loader.load_config", _boom)

    with pytest.raises(gs.GateUnavailable) as excinfo:
        await gs.evaluate_pending()

    assert "fluid evolve run" in str(excinfo.value)
    # The queue is untouched — nothing was evaluated.
    assert [r["target"] for r in gs.pending_proposals()] == ["glm"]


# ─── the round trip ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_synthesizer_draft_reaches_the_queue_and_then_the_prompt(
    install, monkeypatch, tmp_path
):
    """Propose → gate → apply, driven through fluid's own Synthesizer with a
    scripted provider: the one test that proves the channel is open end to
    end rather than merely constructed with the right arguments."""
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.skills import SkillStore

    skills, home = install
    _write_guidance(home, "glm", "Old line: verify writes.")

    body = {
        "content": "Old line: verify writes.\nCall tools directly.",
        "why": "three turns described actions instead of doing them",
        "motivation": {
            "prompt": "list the files in this project",
            "checks": [{"kind": "tool_called", "name": "Glob"}],
        },
    }
    reply = f'<guidance stem="glm">\n{json.dumps(body)}\n</guidance>'

    class _Provider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def stream(self, request):
            self.prompts.append(request.messages[0].content[0].text)

            class Chunk:
                kind = "text_delta"
                payload = {"text": reply}

            yield Chunk()

    provider = _Provider()
    store = SkillStore(skills)
    synth = Synthesizer(
        provider=provider,
        model="synth-model",
        store=store,
        min_new_entries=1,
        session_model=GLM,
        guidance_snapshot=gs.session_guidance_snapshot(GLM),
    )
    store.journal_append({"kind": "user", "text": "please list the files"})
    changed = await synth.run()

    # The pass proposed; it did not write.
    assert changed == ["guidance-proposal:glm"]
    assert GLM in provider.prompts[0]
    assert "Old line: verify writes." in provider.prompts[0]
    assert (home / ".fluid" / "guidance" / "glm.md").read_text(
        encoding="utf-8"
    ) == "Old line: verify writes."
    assert [r["target"] for r in pending_queue(skills)] == ["glm"]

    # The gate applies it, and only then does a session see it.
    await gs.evaluate_pending(runner=ScriptedRunner())

    assert "Call tools directly." in gs.prompt_guidance_blocks(GLM)[0]
