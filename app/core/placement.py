"""Placement resolver — pure resolution of WHERE a session should execute
(V2 Phase 4).

This module answers "given a session's ``placement`` and the current cluster
state, which runner should execute it?" It performs NO execution, NO claiming,
and writes NOTHING: it is a pure function of ``sessions.placement`` +
``installs.capabilities`` + ``checkouts``. Q3 (the turn-claim loop) consumes
these decisions and does the actual pinning/claiming.

Stickiness (OD-6): a session's CLI conversation state lives on the executing
install's local disk, so all turns of one session must execute on the same
runner. This module does not enforce that (it is stateless); the caller pins on
first claim and honors the pin thereafter. A pinned ``{"where":"runner:<id>"}``
that resolves here simply returns that runner when it is online+capable.

Election reuses the shared-DB convention of
``automation_service.should_run_scheduled_here``: the set of online, approved
installs (an approved self is retained through a heartbeat race), and a
deterministic lowest-id winner.

Pinned-vs-elected asymmetry (A10): a ``{"where":"runner:<id>"}`` pin is
validated by online + capability constraints ONLY — no checkout filter — because
checkout rows materialize LAZILY (Phase-3 Step-E adoption), so a backfilled
legacy pin whose owner has not re-materialized the checkout would otherwise
resolve "none" with the owner online and able; checkout honesty is instead
enforced at spawn by the resolver-first refusal (the hazard-3 terminal state).
Elected ("anywhere") placement KEEPS the checkout filter — election means
choosing among runners that actually have the project.

NULL placement + ``instance_id`` (A11): a NULL/blank ``placement`` with a
non-empty ``sessions.instance_id`` is resolved as an IMPLICIT
``runner:<instance_id>`` pin — this is the A9 backfill rule applied at read
time, deterministic on every install. ``create_session`` stamps
``instance_id = <creator>`` at INSERT with placement NULL, so without this
fallback every post-v61 session would resolve install-relative "local" and a
cross-install send would execute on the VIEWER's install with none of the CLI
conversation state (a regression vs the legacy ``is_remote_session`` relay,
which routes on ``instance_id``). The implicit pin is validated exactly like
an explicit one (online + capability, no checkout filter per A10); owner
offline → ``"none"``. Only a NULL placement with an empty/None ``instance_id``
falls through to local-default.

Return shape — ``PlacementDecision``:
  * ``kind == "local"``  — run on this (the local) runner; ``runner_id`` is the
    local ``effective_instance_id``. This is the default for NULL placement
    with no ``instance_id`` owner; a capability miss never blocks local
    placement (OD-8).
  * ``kind == "runner"`` — run on ``runner_id`` (a pinned or elected peer).
  * ``kind == "none"``   — no capable runner is online (degraded state, Q7);
    ``runner_id`` is ``None``.
``reason`` is a short human string for logging.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.db import database
from app.db.checkouts import list_checkouts

logger = logging.getLogger(__name__)

# `where` tokens that name a boolean capability constraint directly (spec §2.1
# `{"where":"anywhere|has_chrome|has_display|runner:<id>"}`). They are folded
# into the constraint set and then elected exactly like "anywhere".
_KNOWN_CAPABILITY_WHERES = frozenset({"has_chrome", "has_display", "docker"})


@dataclass(frozen=True)
class PlacementDecision:
    kind: str  # "local" | "runner" | "none"
    runner_id: Optional[str]
    reason: str


def _normalize_placement(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce the stored ``sessions.placement`` into a dict or None.

    Postgres JSONB hands back a dict; some drivers/the SQLite test shim hand
    back a JSON string. A blank/unparseable value is treated as no placement.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _self_normalized(runner_id: str) -> str:
    """Map this worker's own network device id (or a legacy alias) to ``me``."""
    if not runner_id:
        return runner_id
    try:
        from app.network import runners
        if runners.self_device_id() == runner_id:
            return settings.effective_instance_id
        resolved = runners.resolve_runner_id(runner_id)
        if resolved and resolved == runners.self_device_id():
            return settings.effective_instance_id
    except Exception:  # noqa: BLE001 — never let the network layer break resolution
        pass
    return runner_id


def _installs_map(preloaded: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The install registry snapshot to resolve against.

    ``preloaded`` lets a caller that has ALREADY read the registry hand it in,
    so a batch of resolutions costs ONE read instead of one (in fact two —
    ``capable_runners`` used to read it again for ``_online_runners``) per
    candidate. Passing ``None`` reads it here, exactly as before, which keeps
    every existing caller working standalone. A registry failure yields ``{}``:
    trouble here must never crash resolution (self is re-added by
    ``_online_runners``, so a failed read cannot make every send refuse with
    "no capable device online").
    """
    if preloaded is not None:
        return preloaded
    try:
        return database.get_installs_map()
    except Exception:  # noqa: BLE001 — registry trouble must not crash resolution
        logger.debug("Placement: install registry unavailable", exc_info=True)
        return {}


def _online_runners(installs_map: Optional[Dict[str, Any]] = None) -> set:
    """Online approved installs, plus self unless its own row says unapproved.

    Mirrors ``should_run_scheduled_here``: this code is running, so self is
    alive no matter what its heartbeat row says. Only an explicit registry row
    with ``approved=False`` (quarantine/revocation) drops self; an absent row —
    a boot-time registration race or a failed registry read — must not make
    every send refuse with "no capable device online".
    """
    installs = _installs_map(installs_map)
    online = {
        iid for iid, row in installs.items()
        if row.get("online") and row.get("approved", True)
    }
    me = installs.get(settings.effective_instance_id)
    if me is None or me.get("approved", True):
        online.add(settings.effective_instance_id)
    return online


def _satisfies(install_row: Optional[Dict[str, Any]], constraints: Dict[str, Any]) -> bool:
    """Whether a runner's advertised capabilities satisfy the constraint set.

    A ``True`` constraint requires the capability to be truthy; a ``False``
    constraint is a don't-care; any other value requires equality (e.g.
    ``{"os": "linux"}``). Unknown runner / missing capabilities → a truthy
    requirement fails closed.
    """
    if not constraints:
        return True
    caps: Dict[str, Any] = {}
    if install_row:
        caps = install_row.get("capabilities") or {}
    for key, want in constraints.items():
        have = caps.get(key)
        if want is True:
            if not have:
                return False
        elif want is False:
            continue
        else:
            if have != want:
                return False
    return True


def capable_runners(
    project_v2_id: Optional[str],
    constraints: Optional[Dict[str, Any]] = None,
    installs_map: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Online runners that can execute a session for ``project_v2_id``.

    Filters online installs by:
      (a) an active ``checkouts`` row for the project on that runner — so the
          project is materialized there. Skipped when ``project_v2_id`` is
          falsy (a projectless session imposes no checkout requirement).
      (b) the capability ``constraints`` against ``installs.capabilities``.
      (c) account availability — INTENTIONALLY OMITTED. Claude accounts are
          shared-DB (any install can read/use any account; see
          ``app/db/claude_accounts.py`` and the A8 finding), so there is no
          install-scoped account constraint to apply. Were accounts ever made
          install-local, this is where the per-account filter would go.

    ``installs_map`` (optional) is a PRELOADED install registry snapshot: pass
    the map you already read and this makes NO registry read of its own — the
    same snapshot serves both the capability filter and the online set. Omit it
    and the registry is read here exactly as before.

    Returns a sorted list (deterministic; feeds ``elect_runner``).
    """
    constraints = constraints or {}
    # ONE snapshot for both the capability filter and the online set (this used
    # to be two separate reads of the same registry).
    installs = _installs_map(installs_map)
    online = _online_runners(installs)

    if project_v2_id:
        try:
            checkout_runners = {
                row["runner_id"]
                for row in list_checkouts(project_v2_id)
                if row.get("runner_id")
                and (row.get("status") or "active") == "active"
            }
        except Exception:  # noqa: BLE001 — a checkout read failure yields no candidates
            logger.debug("Placement: checkout lookup failed", exc_info=True)
            checkout_runners = set()
        candidates = online & checkout_runners
    else:
        candidates = set(online)

    return [
        runner_id
        for runner_id in sorted(candidates)
        if _satisfies(installs.get(runner_id), constraints)
    ]


def elect_runner(candidates: List[str]) -> Optional[str]:
    """Deterministic election: the lowest-id candidate (or None if empty).

    Same convention as ``should_run_scheduled_here`` — every install computes
    the same winner from shared state.
    """
    if not candidates:
        return None
    return sorted(candidates)[0]


def resolve_placement(
    session_row: Dict[str, Any],
    installs_map: Optional[Dict[str, Any]] = None,
) -> PlacementDecision:
    """Resolve where ``session_row`` should execute (pure; writes nothing).

    Honors the three canonical placement forms:
      * ``None`` / blank placement → A11: if the row carries a non-empty
        ``instance_id``, that owner is an IMPLICIT ``runner:<instance_id>`` pin
        (validated online+capability like an explicit pin; owner offline →
        ``"none"``); otherwise local-default (this runner).
      * ``{"where": "runner:<id>"}`` → that runner, iff online + capability-
        satisfying, else a ``"none"`` decision. A10: the pin is validated with
        ``capable_runners(None, constraints)`` — online + capability ONLY, NO
        checkout filter (checkout honesty is enforced at spawn by the
        resolver-first refusal, not here).
      * ``{"where": "anywhere"}`` (or a capability ``where`` token / bare
        ``constraints``) → elect the lowest-id capable runner via
        ``capable_runners(project_v2_id, constraints)`` (checkout filter KEPT —
        election chooses among runners that actually have the project), or
        ``"none"``.
    ``session_row`` is a mapping with at least ``placement`` and (optionally)
    ``project_v2_id``.

    ``installs_map`` (optional) is a PRELOADED install registry snapshot. A
    caller resolving many rows at once (the session-list ``degraded``
    derivation, once per queued-with-no-runner row on the page) reads the
    registry ONCE and threads it through, instead of paying two registry reads
    per row. It is handed straight to the single ``capable_runners`` call each
    branch makes (at most one per resolution), so the read stays LAZY: a
    local-default row still touches the registry zero times, exactly as before.
    Omit it and the registry is read as it always was — resolution is unchanged
    either way, since it already ran against one point-in-time snapshot.
    """
    me = settings.effective_instance_id
    placement = _normalize_placement(session_row.get("placement"))

    if not placement:
        # A11: NULL placement falls back to the legacy `instance_id` owner as
        # an IMPLICIT runner pin (the A9 backfill rule applied at read time,
        # deterministic on every install). Validated exactly like the explicit
        # pin arm below: online + capability only (`capable_runners(None, {})`,
        # A10 — no checkout filter), owner offline → "none". Only an ownerless
        # row (instance_id empty/None) is truly local-default.
        # Worker network: a mirrored session carries no instance_id but names
        # its owner device; our own device id means "me".
        owner = _self_normalized((session_row.get("instance_id") or "").strip()
                                 or (session_row.get("owner_device_id") or "").strip())
        if owner:
            if owner in capable_runners(None, {}, installs_map):
                return PlacementDecision(
                    "runner", owner, f"implicit-pin:instance_id {owner} online+capable"
                )
            return PlacementDecision(
                "none", None, f"implicit-pin:instance_id {owner} offline or not capable"
            )
        return PlacementDecision("local", me, "no placement pin — local default")

    where = (placement.get("where") or "").strip()
    constraints: Dict[str, Any] = dict(placement.get("constraints") or {})
    project_v2_id = session_row.get("project_v2_id")

    if where.startswith("runner:"):
        pinned = _self_normalized(where[len("runner:"):].strip())
        # A10: validate the pin against online + capability constraints ONLY
        # (`capable_runners(None, …)`) — NO checkout filter. Checkout rows
        # materialize lazily via Phase-3 Step-E adoption, so a legacy pin whose
        # owner has not yet re-materialized the checkout would otherwise resolve
        # "none" with the owner online and able. Checkout honesty is enforced at
        # spawn by the resolver-first refusal (hazard-3 terminal state).
        if pinned and pinned in capable_runners(None, constraints, installs_map):
            return PlacementDecision("runner", pinned, f"pinned runner {pinned} online+capable")
        return PlacementDecision("none", None, f"pinned runner {pinned or '<blank>'} offline or not capable")

    # A capability-token `where` folds into the constraint set (spec §2.1).
    if where in _KNOWN_CAPABILITY_WHERES:
        constraints[where] = True

    elected = elect_runner(capable_runners(project_v2_id, constraints, installs_map))
    if elected:
        return PlacementDecision("runner", elected, f"elected capable runner {elected}")
    return PlacementDecision("none", None, "no capable runner online")
