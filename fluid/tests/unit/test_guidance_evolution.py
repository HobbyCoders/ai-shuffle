"""Phase 2.3 pins — model guidance as learned data, through the 2.2 gate.

The synthesizer DRAFTS guidance proposals from journal evidence (cheap,
in-band, queue-time validated); the gate DISPOSES out-of-band via
``fluid evolve run`` with live anchor replays. Nothing writes the guidance
directory except the gate's apply step. Same propose-then-curate doctrine
memories follow — one tier up.
"""

import asyncio
import json

import pytest

from fluid.codemode.anchors import ReplayOutcome
from fluid.codemode.continual import Synthesizer
from fluid.codemode.evolution import (
    Proposal,
    draft_guidance_proposal,
    evolution_log,
    parked_proposals,
    pending_queue,
    queue_pending,
)
from fluid.codemode.skills import SkillStore
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
from fluid.config.model import HarnessConfig
from fluid.context.guidance import guidance_sources


class ScriptedProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        reply = self.replies.pop(0)

        class Chunk:
            kind = "text_delta"
            payload = {"text": reply}

        yield Chunk()


GOOD_BODY = {
    "content": "Call tools directly; never describe an intended call.",
    "why": "three journal turns described actions instead of calling tools",
    "motivation": {
        "prompt": "list the files in this project",
        "checks": [{"kind": "tool_called", "name": "Glob"}],
    },
}


def _guidance_tag(stem: str, body: dict) -> str:
    return f'<guidance stem="{stem}">\n{json.dumps(body)}\n</guidance>'


def _synth(tmp_path, replies, **kwargs) -> tuple[Synthesizer, SkillStore, ScriptedProvider]:
    store = SkillStore(tmp_path)
    provider = ScriptedProvider(replies)
    synth = Synthesizer(
        provider=provider, model="synth-model", store=store,
        min_new_entries=1, **kwargs,
    )
    store.journal_append({"kind": "user", "text": "please list the files"})
    return synth, store, provider


# ─── guidance sources ────────────────────────────────────────────────────────


def test_guidance_sources_match_by_stem_substring(tmp_path) -> None:
    directory = tmp_path / ".fluid" / "guidance"
    directory.mkdir(parents=True)
    (directory / "glm.md").write_text("glm rules", encoding="utf-8")
    (directory / "qwen3.md").write_text("qwen rules", encoding="utf-8")
    assert guidance_sources("z-ai/glm-5.3-flash", home=tmp_path) == {
        "glm": "glm rules"
    }
    assert guidance_sources("", home=tmp_path) == {}


# ─── drafting via the synthesizer ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guidance_tag_queues_a_proposal_and_writes_nothing(tmp_path) -> None:
    synth, store, _ = _synth(
        tmp_path,
        [_guidance_tag("glm", GOOD_BODY)],
        session_model="z-ai/glm-5.3-flash",
    )
    changed = await synth.run()
    assert changed == ["guidance-proposal:glm"]
    assert synth.last_error is None
    queued = pending_queue(store.root)
    assert len(queued) == 1
    record = queued[0]
    assert record["target"] == "glm" and record["proposer"] == "synthesizer"
    assert record["motivation"]["checks"][0]["name"] == "Glob"
    # Nothing touched any guidance directory — the gate owns the apply step.
    assert not (tmp_path / ".fluid").exists()
    # The pass consumed its slice like any other.
    assert store.synth_cursor() == 1


@pytest.mark.asyncio
async def test_stem_must_match_the_session_model(tmp_path) -> None:
    synth, store, _ = _synth(
        tmp_path,
        [_guidance_tag("qwen", GOOD_BODY)],
        session_model="z-ai/glm-5.3-flash",
    )
    changed = await synth.run()
    assert changed == []
    assert pending_queue(store.root) == []
    assert "not a substring" in (synth.last_error or "")


@pytest.mark.asyncio
async def test_guidance_without_wired_session_model_is_rejected(tmp_path) -> None:
    synth, store, _ = _synth(tmp_path, [_guidance_tag("glm", GOOD_BODY)])
    changed = await synth.run()
    assert changed == []
    assert pending_queue(store.root) == []
    assert "no session model" in (synth.last_error or "")


@pytest.mark.asyncio
async def test_motivation_is_mandatory_at_queue_time(tmp_path) -> None:
    body = {k: v for k, v in GOOD_BODY.items() if k != "motivation"}
    synth, store, _ = _synth(
        tmp_path,
        [_guidance_tag("glm", body)],
        session_model="z-ai/glm-5.3-flash",
    )
    changed = await synth.run()
    assert changed == []
    assert pending_queue(store.root) == []
    assert "motivation" in (synth.last_error or "")


def test_newer_draft_replaces_pending_for_the_same_stem(tmp_path) -> None:
    first = draft_guidance_proposal(
        stem="glm", body=GOOD_BODY, session_model="z-ai/glm-5.3-flash"
    )
    second = draft_guidance_proposal(
        stem="glm",
        body={**GOOD_BODY, "content": "Revised guidance."},
        session_model="z-ai/glm-5.3-flash",
    )
    assert isinstance(first, Proposal) and isinstance(second, Proposal)
    queue_pending(tmp_path, first)
    queue_pending(tmp_path, second)
    queued = pending_queue(tmp_path)
    assert len(queued) == 1 and queued[0]["content"] == "Revised guidance."


@pytest.mark.asyncio
async def test_prompt_shows_session_model_and_current_guidance(tmp_path) -> None:
    synth, _, provider = _synth(
        tmp_path,
        ["NO_CHANGES"],
        session_model="z-ai/glm-5.3-flash",
        guidance_snapshot={"glm": "House style: terse."},
    )
    await synth.run()
    prompt = provider.prompts[0]
    assert "Session model: z-ai/glm-5.3-flash" in prompt
    assert "### glm.md" in prompt and "House style: terse." in prompt


@pytest.mark.asyncio
async def test_consolidation_ignores_guidance_tags(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("a fact")
    provider = ScriptedProvider([_guidance_tag("glm", GOOD_BODY)])
    synth = Synthesizer(
        provider=provider, model="m", store=store,
        session_model="z-ai/glm-5.3-flash",
    )
    changed = await synth.consolidate()
    assert changed == []
    assert pending_queue(store.root) == []


# ─── gate evaluation of the queue (fluid evolve run) ─────────────────────────


class ScriptedRunner:
    def __init__(self, outcomes: dict[str, ReplayOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}

    async def replay(self, anchor, proposal=None) -> ReplayOutcome:
        return self.outcomes.get(anchor.id, ReplayOutcome(status="ok"))


def _config(tmp_path) -> HarnessConfig:
    return HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {
            "cwd": str(tmp_path),
            "skills_dir": str(tmp_path / "skills"),
        })
    )


def _queue(tmp_path, **body_overrides) -> None:
    proposal = draft_guidance_proposal(
        stem="glm",
        body={**GOOD_BODY, **body_overrides},
        session_model="z-ai/glm-5.3-flash",
    )
    assert isinstance(proposal, Proposal)
    queue_pending(tmp_path / "skills", proposal)


def test_run_pending_commits_through_the_gate(tmp_path) -> None:
    from fluid.cli.evolve import run_pending

    _queue(tmp_path)
    committed, parked = asyncio.run(run_pending(
        _config(tmp_path), runner=ScriptedRunner(), guidance_home=tmp_path,
    ))
    assert committed == ["glm"] and parked == []
    written = tmp_path / ".fluid" / "guidance" / "glm.md"
    assert written.read_text(encoding="utf-8").startswith("Call tools directly")
    assert pending_queue(tmp_path / "skills") == []
    log = evolution_log(tmp_path / "skills")
    assert log and log[-1]["proposer"] == "synthesizer"


def test_run_pending_parks_a_failing_proposal(tmp_path) -> None:
    from fluid.cli.evolve import run_pending

    _queue(tmp_path)
    runner = ScriptedRunner(
        {"motivation": ReplayOutcome(status="ok", failures=("still fails",))}
    )
    committed, parked = asyncio.run(run_pending(
        _config(tmp_path), runner=runner, guidance_home=tmp_path,
    ))
    assert committed == [] and parked == ["glm"]
    assert not (tmp_path / ".fluid" / "guidance" / "glm.md").exists()
    assert pending_queue(tmp_path / "skills") == []
    records = parked_proposals(tmp_path / "skills")
    assert records and records[-1]["stage"] == "improvement"
    assert records[-1]["proposer"] == "synthesizer"
