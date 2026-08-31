"""Phase 2.1/2.2 pins — anchor sets, the replay runner, and the commit gate.

The gate logic is tested with a scripted runner (no engine); the runner is
tested end-to-end with a scripted provider (no network), the same idiom as
``test_cli_smoke.py``. Together they pin the guarded-evolution contract:
validity → improvement → retention, park with evidence, commit with a log.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluid.codemode.anchors import (
    Anchor,
    AnchorStore,
    MAX_ANCHORS,
    ReplayOutcome,
    evaluate_checks,
    validate_anchor,
)
from fluid.codemode.evolution import (
    CommitGate,
    Proposal,
    evolution_log,
    parked_proposals,
)
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
from fluid.config.model import HarnessConfig
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk


# ─── store ───────────────────────────────────────────────────────────────────


def _anchor_fields(**overrides):
    fields = {
        "title": "greets",
        "prompt": "say hello",
        "checks": [{"kind": "contains", "pattern": "hello"}],
    }
    fields.update(overrides)
    return fields


def test_anchor_store_round_trip_and_cap(tmp_path) -> None:
    store = AnchorStore(tmp_path)
    a = store.add(**_anchor_fields())
    assert a.id == "a1" and a.baseline == "unknown"
    assert store.get("a1").prompt == "say hello"
    for i in range(MAX_ANCHORS - 1):
        store.add(**_anchor_fields(title=f"t{i}"))
    with pytest.raises(ValueError, match="cap"):
        store.add(**_anchor_fields(title="over"))
    assert store.delete("a1") and store.get("a1") is None
    store.add(**_anchor_fields(title="fits again"))


def test_anchor_validation_rejects_bad_shapes() -> None:
    assert validate_anchor("", [{"kind": "contains", "pattern": "x"}])
    assert validate_anchor("p", [])
    assert validate_anchor("p", [{"kind": "wat"}])
    assert validate_anchor("p", [{"kind": "contains", "pattern": "("}])
    assert validate_anchor("p", [{"kind": "file", "path": "../escape"}])
    assert validate_anchor(
        "p", [{"kind": "contains", "pattern": "x"}], {"/abs.md": "x"}
    )
    assert validate_anchor("p", [{"kind": "tool_called", "name": "Recall"}]) is None


def test_record_result_sets_baseline(tmp_path) -> None:
    store = AnchorStore(tmp_path)
    a = store.add(**_anchor_fields())
    store.record_result(a.id, passed=True)
    assert store.get(a.id).baseline == "pass"
    store.record_result(a.id, passed=False, detail="tool never fired")
    refreshed = store.get(a.id)
    assert refreshed.baseline == "fail" and "tool never fired" in refreshed.last_result


# ─── checks ──────────────────────────────────────────────────────────────────


def test_evaluate_checks_cover_all_kinds(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("deploys go through CI", encoding="utf-8")
    ok = ReplayOutcome(
        status="ok",
        final_text="Hello — noted.",
        tool_calls=("Note", "Read"),
        workspace=tmp_path,
    )
    checks = [
        {"kind": "contains", "pattern": "hello"},
        {"kind": "not_contains", "pattern": "cannot"},
        {"kind": "tool_called", "name": "Note"},
        {"kind": "file", "path": "notes.md", "pattern": "CI"},
    ]
    assert evaluate_checks(checks, ok) == []
    failures = evaluate_checks(
        [
            {"kind": "contains", "pattern": "goodbye"},
            {"kind": "tool_called", "name": "Recall"},
            {"kind": "file", "path": "missing.md"},
        ],
        ok,
    )
    assert len(failures) == 3


def test_error_outcomes_fail_and_unchanged_passes() -> None:
    checks = [{"kind": "contains", "pattern": "x"}]
    assert evaluate_checks(checks, ReplayOutcome(status="error", error="boom"))
    assert evaluate_checks(checks, ReplayOutcome(status="timeout"))
    assert evaluate_checks(checks, ReplayOutcome(status="unchanged")) == []


# ─── the gate (scripted runner) ──────────────────────────────────────────────


class ScriptedRunner:
    """Replay outcomes keyed by anchor id; unknown ids pass."""

    def __init__(self, outcomes: dict[str, ReplayOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.replayed: list[str] = []

    async def replay(self, anchor: Anchor, proposal=None) -> ReplayOutcome:
        self.replayed.append(anchor.id)
        return self.outcomes.get(anchor.id, ReplayOutcome(status="ok"))


def _proposal(**overrides) -> Proposal:
    fields = dict(
        kind="guidance",
        target="glm",
        content="Keep replies terse.",
        motivation={
            "prompt": "summarize this file",
            "checks": [{"kind": "contains", "pattern": "summary"}],
        },
    )
    fields.update(overrides)
    return Proposal(**fields)


def _passing_anchor_set(tmp_path, n: int = 3) -> AnchorStore:
    store = AnchorStore(tmp_path)
    for i in range(n):
        a = store.add(**_anchor_fields(title=f"behavior {i}"))
        store.record_result(a.id, passed=True)
    return store


@pytest.mark.asyncio
async def test_validity_failure_parks_before_any_replay(tmp_path) -> None:
    runner = ScriptedRunner()
    gate = CommitGate(_passing_anchor_set(tmp_path), runner)
    verdict = await gate.evaluate(
        _proposal(), validator=lambda p: ["stem is wrong"]
    )
    assert not verdict.committed and verdict.stage == "validity"
    assert runner.replayed == []
    parked = parked_proposals(tmp_path)
    assert parked and parked[-1]["stage"] == "validity"
    assert parked[-1]["content"] == "Keep replies terse."
    # The full proposal round-trips out of the parking lot — a requeued
    # proposal must keep its motivation case (live testing hit the loss).
    from fluid.codemode.evolution import proposal_from_dict

    revived = proposal_from_dict(parked[-1])
    assert revived.motivation == _proposal().motivation


@pytest.mark.asyncio
async def test_missing_motivation_parks_unless_waived(tmp_path) -> None:
    runner = ScriptedRunner()
    gate = CommitGate(_passing_anchor_set(tmp_path), runner)
    verdict = await gate.evaluate(_proposal(motivation=None))
    assert not verdict.committed and verdict.stage == "improvement"
    waived = await gate.evaluate(
        _proposal(motivation=None), require_motivation=False
    )
    assert waived.committed


@pytest.mark.asyncio
async def test_unchanged_motivation_is_not_improvement_evidence(tmp_path) -> None:
    """Live testing caught this: a family-scoped guidance edit replayed under
    a mismatched model short-circuits to ``unchanged`` — the motivating case
    never ran, so it must park, not ride through as a pass."""
    runner = ScriptedRunner({"motivation": ReplayOutcome(status="unchanged")})
    gate = CommitGate(_passing_anchor_set(tmp_path), runner)
    verdict = await gate.evaluate(_proposal())
    assert not verdict.committed and verdict.stage == "improvement"
    assert verdict.anchor_results["motivation"] == "unchanged"
    assert any("unaffected" in r for r in verdict.reasons)


@pytest.mark.asyncio
async def test_failing_motivation_parks_at_improvement(tmp_path) -> None:
    runner = ScriptedRunner(
        {"motivation": ReplayOutcome(status="ok", failures=("still fails",))}
    )
    gate = CommitGate(_passing_anchor_set(tmp_path), runner)
    verdict = await gate.evaluate(_proposal())
    assert not verdict.committed and verdict.stage == "improvement"
    # No retention replays were paid for a change that fixes nothing.
    assert runner.replayed == ["motivation"]


@pytest.mark.asyncio
async def test_retention_budget_gates_commit(tmp_path) -> None:
    store = _passing_anchor_set(tmp_path, n=3)
    regressions = {
        "a1": ReplayOutcome(status="ok", failures=("regressed",)),
        "a2": ReplayOutcome(status="ok", failures=("regressed",)),
    }
    gate = CommitGate(store, ScriptedRunner(regressions), budget=1)
    verdict = await gate.evaluate(_proposal())
    assert not verdict.committed and verdict.stage == "retention"
    assert verdict.regressions == 2
    assert parked_proposals(tmp_path)[-1]["stage"] == "retention"

    applied: list[str] = []
    gate = CommitGate(
        store,
        ScriptedRunner({"a1": ReplayOutcome(status="ok", failures=("regressed",))}),
        budget=1,
    )
    verdict = await gate.evaluate(
        _proposal(), apply=lambda p: applied.append(p.target)
    )
    assert verdict.committed and verdict.regressions == 1
    assert applied == ["glm"]
    log = evolution_log(tmp_path)
    assert log and log[-1]["target"] == "glm" and log[-1]["regressions"] == 1


@pytest.mark.asyncio
async def test_gate_skips_unbaselined_and_counts_unchanged_free(tmp_path) -> None:
    store = AnchorStore(tmp_path)
    passing = store.add(**_anchor_fields(title="protected"))
    store.record_result(passing.id, passed=True)
    never_run = store.add(**_anchor_fields(title="unknown baseline"))
    runner = ScriptedRunner({passing.id: ReplayOutcome(status="unchanged")})
    gate = CommitGate(store, runner, budget=0)
    verdict = await gate.evaluate(_proposal())
    assert verdict.committed
    assert verdict.anchor_results[passing.id] == "unchanged"
    assert "skipped" in verdict.anchor_results[never_run.id]
    assert never_run.id not in runner.replayed


# ─── guidance validator + apply ──────────────────────────────────────────────


def test_guidance_validator_enforces_vocabulary_and_caps() -> None:
    from fluid.cli.evolve import guidance_validator

    assert guidance_validator(_proposal(target="BAD STEM"))
    assert guidance_validator(_proposal(content=""))
    assert guidance_validator(_proposal(content="x" * 5_000))
    ghost = guidance_validator(
        _proposal(content="Always call FrobnicateTool before Bash.")
    )
    assert ghost and "FrobnicateTool" in ghost[0]
    assert guidance_validator(
        _proposal(content="Call tools directly; never describe an intended call.")
    ) == []
    # Placeholder letters in prohibition prose are not tool directives — a
    # live synthesizer proposal parked on this false positive.
    assert guidance_validator(
        _proposal(content="Never say 'you could run X' or 'one would invoke Y'.")
    ) == []


def test_apply_guidance_writes_the_file(tmp_path) -> None:
    from fluid.cli.evolve import apply_guidance

    target = apply_guidance(_proposal(), home=tmp_path)
    assert target == tmp_path / ".fluid" / "guidance" / "glm.md"
    assert target.read_text(encoding="utf-8") == "Keep replies terse.\n"


# ─── harvest draft parsing ───────────────────────────────────────────────────


def test_parse_anchor_drafts_validates_and_caps() -> None:
    from fluid.cli.evolve import parse_anchor_drafts

    good = json.dumps(
        {"prompt": "what did we decide?", "checks": [{"kind": "contains", "pattern": "decided"}]}
    )
    bad_json = '<anchor title="broken">{nope}</anchor>'
    bad_checks = (
        '<anchor title="no checks">'
        + json.dumps({"prompt": "p", "checks": []})
        + "</anchor>"
    )
    text = bad_json + bad_checks + "".join(
        f'<anchor title="case {i}">{good}</anchor>' for i in range(7)
    )
    drafts = parse_anchor_drafts(text)
    assert len(drafts) == 5  # cap, invalid ones dropped
    assert drafts[0][0] == "case 0"
    assert parse_anchor_drafts("NO_ANCHORS") == []


# ─── the runner (scripted provider, real engine) ─────────────────────────────


def _chunks_text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _chunks_tool(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk(
            "block_start",
            {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid},
        ),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _config(tmp_path, **overrides) -> HarnessConfig:
    return HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), **overrides})
    )


def test_runner_replays_and_evaluates(tmp_path) -> None:
    from fluid.cli.replay import AnchorRunner

    provider = ScriptedProvider(
        [_chunks_tool("t1", "Note", '{"text": "checking"}'), _chunks_text("noted, hello")]
    )
    runner = AnchorRunner(
        _config(tmp_path, skills_dir=str(tmp_path / "skills")), provider=provider
    )
    anchor = Anchor(
        id="a1",
        title="notes then replies",
        prompt="jot this down",
        checks=(
            {"kind": "contains", "pattern": "hello"},
            {"kind": "tool_called", "name": "Note"},
        ),
    )
    outcome = asyncio.run(runner.replay(anchor))
    assert outcome.status == "ok"
    assert outcome.failures == ()
    assert "Note" in outcome.tool_calls
    # The throwaway workspace is gone; nothing leaked into the real cwd.
    assert outcome.workspace is None


def test_runner_seeds_setup_and_checks_files(tmp_path) -> None:
    from fluid.cli.replay import AnchorRunner

    provider = ScriptedProvider([_chunks_text("the file says CI")])
    runner = AnchorRunner(
        _config(tmp_path, skills_dir=str(tmp_path / "skills")), provider=provider
    )
    anchor = Anchor(
        id="a1",
        title="reads seeded workspace",
        prompt="how do deploys work here?",
        checks=(
            {"kind": "contains", "pattern": "CI"},
            {"kind": "file", "path": "docs/deploy.md", "pattern": "CI"},
        ),
        setup={"docs/deploy.md": "deploys go through CI"},
    )
    outcome = asyncio.run(runner.replay(anchor))
    assert outcome.failures == ()


def test_runner_short_circuits_unaffected_prompts(tmp_path) -> None:
    """A guidance candidate whose stem misses the model compiles to an
    identical prompt — the anchor cannot regress, no model call is spent."""
    from fluid.cli.replay import AnchorRunner

    provider = ScriptedProvider([_chunks_text("never streamed")])
    runner = AnchorRunner(
        _config(tmp_path, skills_dir=str(tmp_path / "skills")), provider=provider
    )
    anchor = Anchor(
        id="a1", title="t", prompt="p",
        checks=({"kind": "contains", "pattern": "x"},),
    )
    proposal = Proposal(kind="guidance", target="zzz-no-such-model", content="Guidance.")
    outcome = asyncio.run(runner.replay(anchor, proposal=proposal))
    assert outcome.status == "unchanged"
    assert provider.calls == 0


def test_runner_applies_matching_candidate_guidance(tmp_path) -> None:
    """A stem matching the model changes the compiled prompt, so the replay
    runs live — with the candidate block present in the system prompt."""
    from fluid.cli.replay import AnchorRunner

    seen_systems: list[str] = []

    class RecordingProvider(ScriptedProvider):
        async def stream(self, request: ProviderRequest):
            seen_systems.append(request.system or "")
            async for chunk in super().stream(request):
                yield chunk

    provider = RecordingProvider([_chunks_text("terse.")])
    runner = AnchorRunner(
        _config(tmp_path, skills_dir=str(tmp_path / "skills")), provider=provider
    )
    anchor = Anchor(
        id="a1", title="t", prompt="p",
        checks=({"kind": "contains", "pattern": "terse"},),
    )
    proposal = Proposal(
        kind="guidance", target="claude", content="House style: terse replies."
    )
    outcome = asyncio.run(runner.replay(anchor, proposal=proposal))
    assert outcome.status == "ok" and outcome.failures == ()
    assert provider.calls == 1
    assert "House style: terse replies." in seen_systems[0]


# ─── config + CLI wiring ─────────────────────────────────────────────────────


def test_anchor_budget_knob_parses_and_round_trips() -> None:
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    assert config.anchor_budget == 1
    strict = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"anchor_budget": 0})
    )
    assert strict.anchor_budget == 0
    assert strict.to_dict()["anchor_budget"] == 0


def test_cli_parses_anchor_and_evolve_commands() -> None:
    from fluid.cli.__main__ import build_parser

    parser = build_parser()
    add = parser.parse_args(
        ["anchors", "add", "say hi", "--expect", "hi", "--tool", "Note"]
    )
    assert add.anchors_command == "add" and add.expect == ["hi"]
    run = parser.parse_args(["anchors", "run", "a1"])
    assert run.anchors_command == "run" and run.id == "a1"
    propose = parser.parse_args(
        ["evolve", "propose", "--stem", "glm", "--content", "x", "--no-motivation"]
    )
    assert propose.evolve_command == "propose" and propose.no_motivation
