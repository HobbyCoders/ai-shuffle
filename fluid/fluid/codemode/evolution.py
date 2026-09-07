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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fluid.codemode.anchors import Anchor, AnchorStore, validate_anchor

DEFAULT_BUDGET = 1

PARKED_FILE = "parked.jsonl"
LOG_FILE = ".evolution-log.jsonl"
# Synthesizer-drafted proposals waiting for gate evaluation (`fluid evolve
# run`). Drafting is cheap and in-band; gating replays anchors against a live
# model, so it never rides a background synthesis pass.
PENDING_FILE = "pending-proposals.jsonl"


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
    # Anchors with a passing baseline behind the retention stage (0 means
    # that stage checked nothing).
    anchors: int = 0


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
        require_anchors: bool = False,
    ) -> GateVerdict:
        """Run the gate; apply and log on commit, park with evidence on fail.

        ``require_anchors`` refuses to commit while no anchor has a passing
        baseline: with nothing to replay, the retention stage passes
        trivially and the only check left is the motivating case the same
        model authored. The default install has no anchors, so a synthesizer
        proposal is refused there unless a human waives this.
        """
        errors = list(validator(proposal)) if validator is not None else []
        if errors:
            return self._park(proposal, "validity", errors, {})

        anchor_results: dict[str, str] = {}
        anchors = self._anchors.list()
        passing = sum(1 for a in anchors if a.baseline == "pass")
        if require_anchors and passing == 0:
            return self._park(
                proposal,
                "retention",
                ["no anchor has a passing baseline, so the retention stage "
                 "cannot detect a regression; add anchors (fluid evolve "
                 "anchors add, then anchors run) or waive with "
                 "--allow-no-anchors"],
                anchor_results,
            )
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
        for anchor in anchors:
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
            anchors=passing,
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
        # The full proposal (motivation included) must survive parking —
        # parked records are the retry evidence, and a requeued proposal
        # without its motivation case would park again as unevidenced (live
        # testing hit exactly that round-trip loss).
        self._append(
            PARKED_FILE,
            {
                "ts": time.time(),
                "stage": stage,
                "reasons": reasons,
                "anchor_results": anchor_results,
                **proposal_to_dict(proposal),
                "evidence": proposal.evidence[:2_000],
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


def proposal_to_dict(proposal: Proposal) -> dict:
    return {
        "kind": proposal.kind,
        "target": proposal.target,
        "content": proposal.content,
        "rationale": proposal.rationale,
        "evidence": proposal.evidence,
        "motivation": proposal.motivation,
        "proposer": proposal.proposer,
    }


def proposal_from_dict(record: dict) -> Proposal:
    return Proposal(
        kind=str(record.get("kind", "guidance")),
        target=str(record.get("target", "")),
        content=str(record.get("content", "")),
        rationale=str(record.get("rationale", "")),
        evidence=str(record.get("evidence", "")),
        motivation=(
            record["motivation"]
            if isinstance(record.get("motivation"), dict)
            else None
        ),
        proposer=str(record.get("proposer", "manual")),
    )


def draft_guidance_proposal(
    *,
    stem: str,
    body: dict,
    session_model: str | None,
    proposer: str = "synthesizer",
) -> Proposal | str:
    """Build a validated guidance proposal from a drafted body, or an error.

    Queue-time validation is the cheap half of the gate: a draft that could
    never load (stem not a substring of the session's model id), carries no
    reproducible motivation, or is malformed is rejected here instead of
    wasting a gate evaluation later. Full validity (screening, vocabulary)
    still runs at gate time.
    """
    from fluid.context.guidance import GUIDANCE_FILE_MAX_CHARS

    stem = (stem or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", stem):
        return f"bad stem {stem!r}"
    if not session_model:
        return "no session model recorded; cannot validate the stem match"
    if stem not in session_model.lower():
        return (
            f"stem {stem!r} is not a substring of the session model "
            f"{session_model!r} — the file would never load"
        )
    if not isinstance(body, dict):
        return "guidance body must be a JSON object"
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return "guidance content is empty"
    if len(content) > GUIDANCE_FILE_MAX_CHARS:
        return f"guidance content exceeds {GUIDANCE_FILE_MAX_CHARS} chars"
    motivation = body.get("motivation")
    if not isinstance(motivation, dict):
        return (
            "a synthesizer proposal must carry a motivation case "
            "(prompt + checks reproducing the failure)"
        )
    from fluid.codemode.anchors import validate_anchor

    error = validate_anchor(
        motivation.get("prompt", ""),
        motivation.get("checks", []),
        motivation.get("setup"),
    )
    if error is not None:
        return f"bad motivation: {error}"
    return Proposal(
        kind="guidance",
        target=stem,
        content=content.strip(),
        rationale=str(body.get("why", ""))[:500],
        evidence=str(body.get("why", ""))[:2_000],
        motivation=motivation,
        proposer=proposer,
    )


def queue_pending(root: str | Path, proposal: Proposal) -> None:
    """Add a proposal to the pending queue.

    One pending proposal per (kind, target): a newer draft for the same
    guidance file replaces the older one — the synthesizer sees the current
    file each pass, so its latest draft supersedes.
    """
    root = Path(root)
    records = [
        r
        for r in pending_queue(root)
        if not (
            r.get("kind") == proposal.kind and r.get("target") == proposal.target
        )
    ]
    records.append({"ts": time.time(), **proposal_to_dict(proposal)})
    _rewrite_pending(root, records)


def pending_queue(root: str | Path) -> list[dict]:
    """Parsed pending-proposal records, oldest first."""
    out: list[dict] = []
    try:
        with (Path(root) / PENDING_FILE).open("r", encoding="utf-8") as fh:
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


def remove_pending(root: str | Path, *, kind: str, target: str) -> None:
    """Drop one (kind, target) from the queue (after a gate verdict — the
    outcome is durable in the evolution log or parked.jsonl either way)."""
    root = Path(root)
    records = [
        r
        for r in pending_queue(root)
        if not (r.get("kind") == kind and r.get("target") == target)
    ]
    _rewrite_pending(root, records)


def _rewrite_pending(root: Path, records: list[dict]) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / PENDING_FILE).open("w", encoding="utf-8") as fh:
            for record in records:
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
