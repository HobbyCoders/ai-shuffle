"""Per-model guidance: the read path, and the commit gate's server-side surface.

This is Phase 2 of the continual harness
(``fluid/docs/research/anchor-gate-design.md``), wired for an embedding host. Fluid
splits the loop across two timescales and the app follows that split exactly:

- **Drafting is cheap and in-band.** The ``Synthesizer`` — constructed in
  ``app/core/provider/harness_adapter.py`` with ``session_model=`` and
  ``guidance_snapshot=`` — may emit a ``<guidance stem="…">`` tag when the
  journal shows a *recurring* model-specific mechanical failure. Fluid
  validates the draft at queue time and appends it to
  ``pending-proposals.jsonl`` in the skills root. **Nothing writes the
  guidance directory here**; the announcement rides the ``[learned]`` line as
  ``guidance-proposal:<stem>``.
- **Gating is expensive and out-of-band.** ``CommitGate`` replays live model
  turns (the motivating case, then every anchor whose baseline passes), so it
  never rides a background synthesis pass. :func:`evaluate_pending` is the
  explicit, operator-triggered entry point — the server-side equivalent of
  ``fluid evolve run``, using fluid's own gate, validator and apply step.

What the human approves is *submission to the gate*, not the edit: the gate
decides commit vs park on replay evidence, and every verdict is durable
(``.evolution-log.jsonl`` on commit, ``parked.jsonl`` otherwise).
:func:`reject_pending` exists so a dismissal is also durable — it parks the
proposal through the gate's free validity stage rather than dropping it, since
parked proposals are the evidence a later, better proposal is built from.

The read path lives here too. Fluid's shipped family blocks stay in code as
factory defaults (``_model_guidance_blocks`` in the harness adapter); learned
guidance files append *after* them, which is what makes a committed proposal
reach the next session. Without that half the gate would write files no
session ever reads.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fluid's guidance files live under ``<home>/.fluid/guidance``. The default
# home is the user's, matching fluid's own default — an operator running
# ``fluid evolve run`` against this install's skills root must commit into the
# directory the app reads, or the loop dead-ends. FLUID_GUIDANCE_HOME
# relocates both halves together.
GUIDANCE_HOME_ENV = "FLUID_GUIDANCE_HOME"


class GateUnavailable(RuntimeError):
    """Gate evaluation cannot run here (no replay runner could be built)."""


def guidance_home() -> Path:
    """The home whose ``.fluid/guidance`` holds this install's guidance files."""
    override = (os.environ.get(GUIDANCE_HOME_ENV) or "").strip()
    return Path(override) if override else Path.home()


def guidance_directory() -> Path:
    """``<home>/.fluid/guidance`` — where committed proposals land."""
    from fluid.context.guidance import guidance_dir

    return guidance_dir(guidance_home())


def _skills_root() -> Path:
    """The continual store's root — the queue and the audit logs live here."""
    from app.core.fluid_memory import skills_root

    return skills_root()


# ─── read path ───────────────────────────────────────────────────────────────


def session_guidance_snapshot(model: Optional[str]) -> Dict[str, str]:
    """Raw ``{stem: content}`` guidance files matching ``model``.

    Exactly what :func:`prompt_guidance_blocks` renders into this session's
    prompt, in the form the synthesizer needs: unscreened and untruncated,
    because a proposal replaces the *whole file* and an amendment written
    against a truncated view would silently delete the tail.

    The shipped family blocks are deliberately absent. They are factory
    defaults living in code (fluid's scope decision: migrating them to data
    would change fresh-install behavior for zero evolution benefit); learned
    guidance appends after them and fully owns any new family. Snapshotting
    them would invite the synthesizer to copy factory text into a file that
    then renders twice.
    """
    if not model:
        return {}
    try:
        from fluid.context.guidance import guidance_sources

        return dict(guidance_sources(model, home=guidance_home()))
    except Exception:  # noqa: BLE001 — a missing/unreadable dir is not a turn failure
        logger.warning(
            "[guidance] snapshot failed for model %r", model, exc_info=True
        )
        return {}


def prompt_guidance_blocks(model: Optional[str]) -> Tuple[str, ...]:
    """Learned guidance blocks for ``model``, screened and capped, or ``()``.

    Appended after the shipped family blocks, sorted by stem — fluid's own
    ordering, so the prompt prefix stays byte-stable and cacheable.
    """
    if not model:
        return ()
    try:
        from fluid.cli.prompt import load_guidance_files

        return tuple(load_guidance_files(model, home=guidance_home()))
    except Exception:  # noqa: BLE001 — degrade to the shipped blocks alone
        logger.warning(
            "[guidance] loading guidance files failed for model %r",
            model, exc_info=True,
        )
        return ()


# ─── inspection ──────────────────────────────────────────────────────────────


def pending_proposals() -> List[Dict[str, Any]]:
    """Queued proposals awaiting gate evaluation, oldest first.

    One per ``(kind, target)`` — a newer draft for the same guidance file
    supersedes the older one, because the synthesizer sees the current file
    each pass.
    """
    from fluid.codemode.evolution import pending_queue

    return list(pending_queue(_skills_root()))


def parked_proposals() -> List[Dict[str, Any]]:
    """Proposals the gate refused, with the failing stage and its evidence."""
    from fluid.codemode.evolution import parked_proposals as _parked

    return list(_parked(_skills_root()))


def evolution_log() -> List[Dict[str, Any]]:
    """Committed harness-state changes: what changed, and at what anchor cost."""
    from fluid.codemode.evolution import evolution_log as _log

    return list(_log(_skills_root()))


def anchors() -> List[Dict[str, Any]]:
    """The anchor set retention is measured against, as JSON-ready records.

    Anchors are managed with ``fluid anchors`` (add / run / harvest): harvest
    needs a human curator per fluid's design, and ``run`` records baselines
    with live replays. The app only reads them, so an operator can see what
    the gate will hold a proposal to.
    """
    from fluid.codemode.anchors import AnchorStore

    return [
        {
            "id": a.id,
            "title": a.title,
            "prompt": a.prompt,
            "checks": [dict(c) for c in a.checks],
            "model": a.model,
            "source": a.source,
            "baseline": a.baseline,
            "last_run_at": a.last_run_at,
            "last_result": a.last_result,
        }
        for a in AnchorStore(_skills_root()).list()
    ]


# ─── disposition ─────────────────────────────────────────────────────────────


async def reject_pending(
    target: str,
    *,
    kind: str = "guidance",
    reason: str = "rejected by an operator",
) -> bool:
    """Dismiss a queued proposal, durably. True if one was found.

    Parked, not deleted: the record (motivation included) is what a later,
    better proposal is built from. This runs the gate's *validity* stage with
    a validator that always refuses — the first stage, and free, so the park
    record is written by fluid's own bookkeeping without a single replay. The
    runner is never reached, which is why ``None`` stands in for it.
    """
    from fluid.codemode.anchors import AnchorStore
    from fluid.codemode.evolution import (
        CommitGate,
        proposal_from_dict,
        remove_pending,
    )

    root = _skills_root()
    record = next(
        (
            r
            for r in pending_proposals()
            if r.get("kind") == kind and r.get("target") == target
        ),
        None,
    )
    if record is None:
        return False
    gate = CommitGate(AnchorStore(root), None)
    await gate.evaluate(
        proposal_from_dict(record), validator=lambda _p: [reason]
    )
    remove_pending(root, kind=kind, target=target)
    return True


async def evaluate_pending(
    *,
    runner: Any | None = None,
    budget: Optional[int] = None,
    cwd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run every queued proposal through fluid's commit gate. Operator-only.

    Mirrors ``fluid.cli.evolve.run_pending`` — the same ``CommitGate``, the
    same ``guidance_validator`` and ``apply_guidance``, and the same
    ``require_motivation=True`` (the synthesizer never gets the waiver) —
    returning structured verdicts instead of printing them.

    Each verdict is durable before the proposal leaves the queue, so an
    interrupted run loses nothing but the un-evaluated tail. Expensive by
    construction: every evaluation replays live model turns, which is why
    nothing calls this from a session or a background synthesis pass.

    Raises :class:`GateUnavailable` when no replay runner was supplied and
    none can be built from fluid's configuration.
    """
    from fluid.cli.evolve import apply_guidance, guidance_validator
    from fluid.codemode.anchors import AnchorStore
    from fluid.codemode.evolution import (
        CommitGate,
        DEFAULT_BUDGET,
        proposal_from_dict,
        remove_pending,
    )

    root = _skills_root()
    records = pending_proposals()
    if not records:
        return []
    if runner is None:
        runner, config_budget = _build_runner(cwd)
        if budget is None:
            budget = config_budget
    if budget is None:
        budget = DEFAULT_BUDGET

    home = guidance_home()
    gate = CommitGate(AnchorStore(root), runner, budget=budget)
    verdicts: List[Dict[str, Any]] = []
    for record in records:
        proposal = proposal_from_dict(record)
        if proposal.kind != "guidance":
            # Unknown kinds stay queued for a newer fluid, exactly as the CLI
            # leaves them.
            continue
        verdict = await gate.evaluate(
            proposal,
            validator=guidance_validator,
            apply=lambda p: apply_guidance(p, home=home),
            require_motivation=True,
        )
        remove_pending(root, kind=proposal.kind, target=proposal.target)
        verdicts.append(
            {
                "kind": proposal.kind,
                "target": proposal.target,
                "proposer": proposal.proposer,
                "committed": verdict.committed,
                "stage": verdict.stage,
                "reasons": list(verdict.reasons),
                "anchor_results": dict(verdict.anchor_results),
                "regressions": verdict.regressions,
                "budget": verdict.budget,
            }
        )
    return verdicts


def _build_runner(cwd: Optional[str]) -> Tuple[Any, Optional[int]]:
    """Fluid's own ``AnchorRunner`` over this install's stores.

    Replays build a real fluid engine, so they need fluid's provider
    configuration (``~/.fluid/config.toml`` + environment) — configuration the
    app does not otherwise keep. When it cannot produce one, say so instead of
    half-running the gate.
    """
    try:
        from fluid.cli.replay import AnchorRunner
        from fluid.config.loader import load_config

        config = load_config(
            cwd or ".", overrides={"skills_dir": str(_skills_root())}
        )
        return (
            AnchorRunner(config, guidance_source=guidance_directory()),
            getattr(config, "anchor_budget", None),
        )
    except Exception as exc:  # noqa: BLE001
        raise GateUnavailable(
            "no anchor-replay runner: gate evaluation needs fluid's provider "
            f"configuration ({exc}). Run `fluid evolve run` with "
            f"FLUID_SKILLS_DIR={_skills_root()} instead."
        ) from exc
