"""The commit gate — guarded harness evolution (HCL), Phase 2.2.

Memories and skills keep their existing lighter gates (selftest verification,
caps, screening). This module gates the state that *instructs* rather than
informs: guidance blocks today, description phrasings and defaults later. A
:class:`Proposal` commits only after three stages, run in cost order:

1. **validity** — free, per-kind checks injected by the caller (the CLI layer
   owns the guidance vocabulary; ``codemode`` keeps not importing ``cli``);
2. **improvement** — the proposal's ``motivation`` (an inline anchor-shaped
   case reproducing the failure that motivated the change) must pass when
   replayed WITH the candidate applied;
3. **retention** — every anchor whose recorded baseline is ``pass`` replays
   with the candidate; regressions must stay within ``budget`` (HCL's B,
   default 1 — their result: a moderate budget beats both strict stability
   and unguarded plasticity).

Verdicts are durable either way: commits append to ``.evolution-log.jsonl``
(what changed, per-anchor results, budget spent) and the change is applied by
the caller-supplied ``apply``; failures append to ``parked.jsonl`` with the
failing stage and evidence — parked proposals inform a later, better
proposal, they are never silently dropped.

The replay runner is injected (anything with
``async replay(anchor, proposal=None) -> ReplayOutcome``); the real one lives
at the CLI layer, tests script one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fluid.codemode.anchors import Anchor, AnchorStore, validate_anchor

DEFAULT_BUDGET = 1

PARKED_FILE = "parked.jsonl"
LOG_FILE = ".evolution-log.jsonl"


@dataclass(frozen=True, kw_only=True)
class Proposal:
    """One proposed harness-state change.

    ``motivation`` is an anchor-shaped dict (``prompt``, ``checks``, optional
    ``setup``/``tools``/``model``) reproducing the failure the change fixes —
    the improvement stage replays it with the candidate applied. A proposal
    without one is parked as unevidenced unless the caller explicitly waives
    the requirement (a human privilege; the synthesizer never gets it).
    """

    kind: str  # "guidance" today
    target: str  # e.g. the guidance file stem ("glm")
    content: str  # the full proposed state (file content)
    rationale: str = ""
    evidence: str = ""  # journal/session excerpts backing the change
    motivation: dict | None = None
    proposer: str = "manual"  # "manual" | "synthesizer"


@dataclass(frozen=True, kw_only=True)
class GateVerdict:
    committed: bool
    stage: str | None = None  # failing stage when not committed
    reasons: tuple[str, ...] = ()
    anchor_results: dict[str, str] = field(default_factory=dict)
    regressions: int = 0
    budget: int = DEFAULT_BUDGET


class CommitGate:
    def __init__(
        self,
        anchors: AnchorStore,
        runner: Any,
        *,
        budget: int = DEFAULT_BUDGET,
        log_root: str | Path | None = None,
    ) -> None:
        self._anchors = anchors
        self._runner = runner
        self._budget = max(0, int(budget))
        self._log_root = Path(log_root) if log_root is not None else anchors.root

    # ─── the three stages ────────────────────────────────────────────────
    async def evaluate(
        self,
        proposal: Proposal,
        *,
        validator: Callable[[Proposal], list[str]] | None = None,
        apply: Callable[[Proposal], None] | None = None,
        require_motivation: bool = True,
    ) -> GateVerdict:
        """Run the gate; apply and log on commit, park with evidence on fail."""
        errors = list(validator(proposal)) if validator is not None else []
        if errors:
            return self._park(proposal, "validity", errors, {})

        anchor_results: dict[str, str] = {}
        if proposal.motivation is None:
            if require_motivation:
                return self._park(
                    proposal,
                    "improvement",
                    ["no motivating case: a change without evidence of the "
                     "failure it fixes cannot show improvement"],
                    anchor_results,
                )
        else:
            motivation = self._motivation_anchor(proposal.motivation)
            if isinstance(motivation, str):
                return self._park(
                    proposal, "improvement", [f"bad motivation: {motivation}"],
                    anchor_results,
                )
            outcome = await self._runner.replay(motivation, proposal=proposal)
            if outcome.status == "unchanged":
                # The candidate did not alter the motivating case's compiled
                # prompt — an un-exercised case proves nothing, and committing
                # on it would let any change ride in on an unrelated anchor
                # (live testing caught exactly this: a family-scoped guidance
                # edit under a mismatched model committed with zero evidence).
                anchor_results["motivation"] = "unchanged"
                return self._park(
                    proposal,
                    "improvement",
                    ["the motivating case is unaffected by the change (its "
                     "compiled prompt is identical) — it cannot evidence "
                     "improvement; check the target/model pairing"],
                    anchor_results,
                )
            failures = list(outcome.failures)
            anchor_results["motivation"] = (
                "pass" if not failures else "; ".join(failures)[:400]
            )
            if failures:
                return self._park(
                    proposal,
                    "improvement",
                    ["the motivating case still fails with the change applied:",
                     *failures],
                    anchor_results,
                )

        regressions = 0
        retention_reasons: list[str] = []
        for anchor in self._anchors.list():
            if anchor.baseline != "pass":
                anchor_results[anchor.id] = f"skipped (baseline {anchor.baseline})"
                continue
            outcome = await self._runner.replay(anchor, proposal=proposal)
            if outcome.status == "unchanged":
                anchor_results[anchor.id] = "unchanged"
                continue
            failures = list(outcome.failures)
            if failures:
                regressions += 1
                anchor_results[anchor.id] = "; ".join(failures)[:400]
                retention_reasons.append(
                    f"{anchor.id} ({anchor.title}): {failures[0]}"
                )
            else:
                anchor_results[anchor.id] = "pass"
        if regressions > self._budget:
            return self._park(
                proposal,
                "retention",
                [f"{regressions} anchor regressions exceed budget "
                 f"{self._budget}:", *retention_reasons],
                anchor_results,
                regressions=regressions,
            )

        if apply is not None:
            apply(proposal)
        verdict = GateVerdict(
            committed=True,
            anchor_results=anchor_results,
            regressions=regressions,
            budget=self._budget,
        )
        self._append(
            LOG_FILE,
            {
                "ts": time.time(),
                "kind": proposal.kind,
                "target": proposal.target,
                "proposer": proposal.proposer,
                "rationale": proposal.rationale,
                "anchor_results": anchor_results,
                "regressions": regressions,
                "budget": self._budget,
            },
        )
        return verdict

    # ─── bookkeeping ─────────────────────────────────────────────────────
    @staticmethod
    def _motivation_anchor(motivation: dict) -> Anchor | str:
        """Build the inline improvement case, or return an error string."""
        prompt = motivation.get("prompt", "")
        checks = motivation.get("checks", [])
        error = validate_anchor(prompt, checks, motivation.get("setup"))
        if error is not None:
            return error
        return Anchor(
            id="motivation",
            title="motivating case",
            prompt=prompt,
            checks=tuple(checks),
            setup=dict(motivation.get("setup") or {}),
            tools=(
                tuple(motivation["tools"])
                if motivation.get("tools") is not None
                else None
            ),
            model=motivation.get("model") or None,
        )

    def _park(
        self,
        proposal: Proposal,
        stage: str,
        reasons: list[str],
        anchor_results: dict[str, str],
        *,
        regressions: int = 0,
    ) -> GateVerdict:
        self._append(
            PARKED_FILE,
            {
                "ts": time.time(),
                "stage": stage,
                "reasons": reasons,
                "kind": proposal.kind,
                "target": proposal.target,
                "proposer": proposal.proposer,
                "rationale": proposal.rationale,
                "evidence": proposal.evidence[:2_000],
                "content": proposal.content,
                "anchor_results": anchor_results,
            },
        )
        return GateVerdict(
            committed=False,
            stage=stage,
            reasons=tuple(reasons),
            anchor_results=anchor_results,
            regressions=regressions,
            budget=self._budget,
        )

    def _append(self, name: str, record: dict) -> None:
        try:
            self._log_root.mkdir(parents=True, exist_ok=True)
            with (self._log_root / name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def parked_proposals(root: str | Path) -> list[dict]:
    """Parsed ``parked.jsonl`` records, oldest first (malformed lines skipped)."""
    out: list[dict] = []
    try:
        with (Path(root) / PARKED_FILE).open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        pass
    return out


def evolution_log(root: str | Path) -> list[dict]:
    """Parsed ``.evolution-log.jsonl`` records, oldest first."""
    out: list[dict] = []
    try:
        with (Path(root) / LOG_FILE).open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        pass
    return out
