"""Task-tool dispatch — the unified agent-orchestration surface (rulings A6-A10).

The ``Task`` tool (fluid builtin on native legs, ``mcp__builtins__Task`` on the
delegated leg) resolves its target against the unified ``agents`` table and
runs it as a worker session via :func:`app.core.provider_agent.dispatch_worker`
— the proven ProviderAgent machinery (real session rows, approval routing,
supervisor teardown, progress feed, bounded report).

What this module adds on top of raw ``dispatch_worker``:

* target resolution — agent id first, then unambiguous case-insensitive name;
* NO invoke allowlist (2026-08-17 ruling, superseding A10) — dispatch is never
  blocked by the caller's ``enabled_agents`` / ``agent_invoke_mode``. Those two
  are a CONTEXT-LOADING concern only: they decide which agent definitions are
  advertised in the caller's prompt (see :mod:`app.core.agent_context`), and
  any agent that resolves here — DB row or file/plugin definition — can be
  dispatched by name whether it was declared or not;
* nesting depth (A9) — max ``settings.task_tool_max_depth`` (5) levels of
  agent-invokes-agent, counted by walking worker-created ancestor sessions;
* cycle detection (A9) — an agent already active in the ancestor chain cannot
  be dispatched again beneath itself;
* worker budget (A9) — at most ``settings.task_tool_max_workers`` live
  workers per root session tree;
* root routing (A9) — approvals, progress cards, and supervisor registration
  land on the ROOT session (the one with a human watching), while
  project/worktree/cwd inheritance still reads the direct caller;
* modes (A8) — ``sync`` blocks and returns the worker's report;
  ``background`` returns the worker session id immediately (collect later via
  TaskList/TaskOutput — background workers survive the caller's turn end);
* LEG RESOLUTION (2026-08-17) — which provider/model the worker actually runs
  on, including the cross-provider ``model`` override and the credential
  pre-flight that makes a doomed dispatch fail synchronously. The full rule
  table lives above :class:`ModelBinding`; read it before changing any of it.

File-defined agents (Claude-ecosystem compat, ruling CE6)
---------------------------------------------------------
On a native (fluid) leg the engine reads the user's ``.claude/agents/**/*.md``
definitions — project, global, and namespaced ``<plugin>:<agent>`` ones — and
publishes them on ``ctx.extra["agent_registry"]``. fluid never spawns anything;
this module is what makes those names dispatchable, as a FALLBACK behind the
app's own ``agents`` table (host wins a name collision, matching fluid's own
doctrine and its registry-seam precedence).

The DELEGATED (claude CLI) leg runs no fluid engine, so nothing publishes that
registry there. Its bridged ``mcp__builtins__Task`` runs the same discovery
itself from the session's cwd (``app.mcp.builtin_tools._discover_agent_registry``)
and passes the result to ``agent_registry`` below — the two legs therefore
resolve the same names, which is what the adapter's shared agents index
promises the model on both.

A file agent has no ``agents`` row, so it borrows the CALLER's profile (and with
it the provider binding, tools and MCP surface) and its markdown body rides the
worker's system prompt as an override block. Consequences, deliberate:

* ``tools:`` in the frontmatter is ENFORCED (ruling CE12), not advisory: the
  list rides the worker's turn as a ``tool_allowlist`` override and becomes
  ``NativeLoop(allowed_tools=...)`` on the native leg — a filtered tool's schema
  never reaches the model, ToolSearch will not load it, and a call naming it
  comes back as an in-context error. An empty/absent list inherits the caller's
  full toolset. On the DELEGATED (claude CLI) leg the same list is passed as
  ``--allowed-tools``, which the CLI reads as permission rules rather than a
  surface restriction — see :func:`app.core.provider.harness_adapter` for the
  exact per-leg semantics;
* ``model:`` goes through the same leg resolution every dispatch does (see the
  rule table above :class:`ModelBinding`) — so a definition written for Claude
  runs on an OpenAI-bound session by routing to whichever configured provider
  serves that model or its family. Only when NOTHING can serve it is the model
  dropped with a note: a file agent must still spawn, never fail on its model;
* the profile-id cycle check does not apply (the worker runs on the caller's own
  profile by construction); depth and worker budget both do.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Hard ceiling on parent-chain walks — above max depth anyway.
_CHAIN_LIMIT = 12

#: Background dispatch tasks, kept referenced so asyncio never GCs them.
_BACKGROUND_TASKS: set = set()


# ---------------------------------------------------------------------------
# Chain / depth helpers
# ---------------------------------------------------------------------------

def _session_row(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        from app.db import database

        return database.get_session(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("task_dispatch: session read failed for %s", session_id, exc_info=True)
        return None


def _first_turn_source(session_id: str) -> Optional[str]:
    """The ``source`` of a session's first turn — worker sessions are born
    with a ``provider_agent`` turn; forks/user sessions are not."""
    try:
        from app.db.base import get_db

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source FROM turns WHERE session_id = %s "
                "ORDER BY created_at ASC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            return row["source"] if row else None
    except Exception:  # noqa: BLE001
        return None


def _is_worker_session(session_id: str) -> bool:
    from app.core import provider_agent

    if provider_agent.is_worker_session(session_id):
        return True
    return _first_turn_source(session_id) == provider_agent.WORKER_TURN_SOURCE


def caller_chain(session_id: str) -> Tuple[List[Dict[str, Any]], str, int]:
    """Walk the parent chain upward from ``session_id``.

    Returns ``(worker_ancestor_rows, root_session_id, depth)`` where ``depth``
    is the number of worker sessions in the chain INCLUDING ``session_id``
    itself (0 for a top-level human session), and ``root_session_id`` is the
    top non-worker session (falls back to the highest reachable ancestor).
    """
    rows: List[Dict[str, Any]] = []
    depth = 0
    current_id = session_id
    root_id = session_id
    for _ in range(_CHAIN_LIMIT):
        row = _session_row(current_id)
        if row is None:
            break
        if current_id != session_id:
            rows.append(row)
        if _is_worker_session(current_id):
            depth += 1
            parent = row.get("parent_session_id")
            if parent:
                root_id = parent
                current_id = parent
                continue
            break
        # First non-worker session is the root — stop walking (a fork's
        # parent link above a human session is lineage, not nesting).
        root_id = current_id
        break
    return rows, root_id, depth


def _chain_agent_ids(session_id: str) -> List[str]:
    """Profile (agent) ids active in the caller chain, caller first."""
    ids: List[str] = []
    current_id: Optional[str] = session_id
    for _ in range(_CHAIN_LIMIT):
        if not current_id:
            break
        row = _session_row(current_id)
        if row is None:
            break
        pid = row.get("profile_id")
        if pid:
            ids.append(pid)
        if not _is_worker_session(current_id):
            break
        current_id = row.get("parent_session_id")
    return ids


def _live_worker_count_for_root(root_session_id: str) -> int:
    """Live Task/ProviderAgent workers anywhere under ``root_session_id``."""
    from app.core import provider_agent_supervisor as sup

    count = 0
    live = getattr(sup, "_LIVE", {})
    for parent_id, bucket in list(live.items()):
        _, root, _ = caller_chain(parent_id)
        if root == root_session_id or parent_id == root_session_id:
            count += len(bucket)
    return count


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target_agent(ref: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve ``ref`` (agent id, else unambiguous name) to an agent row.

    Returns ``(agent_row, error_message)`` — exactly one is non-None.
    """
    from app.db import database

    ref = (ref or "").strip()
    if not ref:
        return None, "Task requires a target agent (id or name)."
    row = database.get_profile(ref)
    if row:
        return row, None
    matches = [
        a for a in database.get_all_profiles()
        if (a.get("name") or "").strip().lower() == ref.lower()
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        ids = ", ".join(sorted(a["id"] for a in matches))
        return None, f"Agent name '{ref}' is ambiguous — use an id: {ids}"
    return None, f"Unknown agent: '{ref}'. List agents via the agents API/UI."


def _def_field(definition: Any, field: str, default: Any = None) -> Any:
    """One field off an agent definition — dataclass attr or mapping key.

    The registry holds fluid ``AgentDefinition`` records, but this module never
    imports fluid (it also serves the delegated leg, which has no engine
    objects at all): duck-typing keeps the seam honest and the tests trivial.
    """
    if isinstance(definition, dict):
        value = definition.get(field, default)
    else:
        value = getattr(definition, field, default)
    return default if value is None else value


def resolve_registry_agent(ref: str, registry: Any) -> Optional[Any]:
    """A file/plugin agent definition matching ``ref``, or None.

    Exact name first, then a unique case-insensitive match — the same
    forgiveness ``resolve_target_agent`` grants app agents, because the model
    reads these names out of a prompt index and re-types them.
    """
    if not isinstance(registry, dict) or not registry:
        return None
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref in registry:
        return registry[ref]
    matches = [d for name, d in registry.items() if name.strip().lower() == ref.lower()]
    return matches[0] if len(matches) == 1 else None


def _file_agent_prompt(definition: Any) -> str:
    """The worker's system-prompt override block for a file-defined agent."""
    name = str(_def_field(definition, "name", "") or "agent")
    path = str(_def_field(definition, "path", "") or "")
    body = str(_def_field(definition, "body", "") or "").strip()
    tools = tuple(_def_field(definition, "tools", ()) or ())

    lines = [
        f"## Agent definition — you are running as the '{name}' agent",
        (
            f"These instructions come from the agent definition at {path}. "
            "They describe the role you are dispatched for and take precedence "
            "over your profile's general instructions for this task."
            if path
            else
            "The instructions below define the role you are dispatched for and "
            "take precedence over your profile's general instructions for this "
            "task."
        ),
    ]
    if tools:
        lines.append(
            "This definition restricts the agent to these tools: "
            + ", ".join(str(t) for t in tools)
            + ". The restriction is enforced by the runtime — tools outside the "
            "list are not part of your tool surface and calling one fails."
        )
    if body:
        lines.append(body)
    return "\n\n".join(lines)


def _definition_tool_allowlist(definition: Any) -> Optional[List[str]]:
    """The definition's ``tools:`` frontmatter as a worker tool allowlist.

    Ruling CE12: a non-empty list is the worker's ENTIRE tool surface; an empty
    or absent list means "inherit everything the caller has" and is signalled by
    None (fluid's ``allowed_tools=None`` = unrestricted). Blank entries are
    dropped; nothing is ever added back — a definition that omits an "essential"
    tool simply does not get it.
    """
    names = [
        str(t).strip()
        for t in (_def_field(definition, "tools", ()) or ())
        if str(t).strip()
    ]
    return names or None


def _caller_provider_id(caller_row: Dict[str, Any], caller_profile_id: Optional[str]) -> Optional[str]:
    """The provider the caller is bound to — session row first, profile after."""
    provider_id = (caller_row or {}).get("provider_id")
    if provider_id:
        return str(provider_id)
    if not caller_profile_id:
        return None
    try:
        from app.db import database

        profile = database.get_profile(caller_profile_id) or {}
    except Exception:  # noqa: BLE001 — binding lookup is best-effort
        return None
    return ((profile.get("config") or {}).get("provider")) or None


def _validate_target(agent: Dict[str, Any]) -> Optional[str]:
    """Provider admission for the target agent. None = OK.

    MODEL admission is NOT decided here (2026-08-17 ruling): a ``model``
    override is intentionally cross-provider, so the slug decides the provider
    rather than the provider gating the slug — see
    :func:`resolve_dispatch_binding`.
    """
    from app.core.environment import is_provider_servable
    from app.db import database

    config = agent.get("config") or {}
    provider_id = config.get("provider")
    if not provider_id:
        # Delegated Claude-subscription leg — always a valid worker target.
        return None
    try:
        provider = database.get_model_provider(provider_id)
    except Exception:  # noqa: BLE001
        provider = None
    if not provider:
        return f"Agent '{agent['id']}' is bound to unknown provider '{provider_id}'."
    if not provider.get("enabled"):
        return f"Agent '{agent['id']}' is bound to disabled provider '{provider_id}'."
    if not is_provider_servable(provider):
        return (
            f"Agent '{agent['id']}' is bound to provider '{provider_id}' "
            "which cannot serve worker sessions."
        )
    return None


# ---------------------------------------------------------------------------
# Worker leg resolution (ruling 2026-08-17, as refined)
# ---------------------------------------------------------------------------
#
# THE RULE TABLE. Read this before touching anything below.
#
# 1. DEFAULT = THE TARGET'S OWN BINDING. A dispatched agent runs its own
#    configured provider AND model, even when the caller sits on a different
#    provider. A ChatGPT-bound caller spawning an OpenRouter-bound agent gets
#    exactly the agent's OpenRouter binding — no rerouting, no override needed.
#    Native legs mix and match freely.
#
# 2. THE ONE SPECIAL CASE — the Claude-subscription (delegated CLI) leg. It is
#    the only leg that does not mix and match. So: when the TARGET resolves to
#    the Claude-sub leg (no provider row) and the CALLER is NOT itself on the
#    Claude-sub leg, the target's model is re-routed onto a compatible
#    configured provider — exact curated slug first, then the same model FAMILY
#    (``model_registry.get_model_family``, the app's own family vocabulary),
#    with the caller's own provider preferred among ties. If no configured
#    provider can serve the model or its family, the delegated CLI worker is
#    spawned anyway (it does function) and the result carries a note saying so.
#    A Claude-sub caller dispatching a Claude-sub target is left alone.
#
# 3. AN EXPLICIT ``model`` OVERRIDE is the exception path, not the routine. It
#    is intentionally cross-provider: the SLUG picks the leg, so no user ever
#    needs duplicate per-provider agents. Resolution order is the target's own
#    provider, then any configured servable provider curating the exact slug
#    (caller's provider preferred), then — for a Claude-family slug — rule 2's
#    preference chain, and finally a SYNCHRONOUS failure when nothing can serve
#    it (nothing is spawned, so the caller learns immediately).
#
# 4. Whatever leg is resolved, its credentials are pre-flighted (``_credential_gap``)
#    before anything is spawned.

@dataclass
class ModelBinding:
    """Which leg a dispatched worker must actually run on.

    ``provider_id`` is the ``model_providers`` row the worker session is BOUND
    to — ``None`` means the delegated Claude-subscription leg (no row).
    ``model_id`` is the model to pin for the turn, or ``None`` for "inherit the
    target's configured model" (rule 1's untouched default). When ``error`` is
    set the dispatch fails synchronously and nothing is spawned.
    """

    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None


def _servable_providers_curating(model_id: str) -> List[Dict[str, Any]]:
    """Enabled + servable provider rows whose curated list holds ``model_id``.

    Reuses the two existing admission helpers rather than re-deriving them:
    :func:`app.core.environment.is_provider_servable` (the cross-provider WORKER
    admission rule) and :func:`app.core.provider_agent.curated_model_ids` (the
    one defensive parser for ``models_json``). Sorted by ``(name, id)`` so a
    slug curated by several providers always resolves to the same one.
    """
    from app.core.environment import is_provider_servable
    from app.core.provider_agent import curated_model_ids
    from app.db import database

    try:
        rows = database.get_enabled_model_providers()
    except Exception:  # noqa: BLE001 — a lookup failure reads as "nobody curates it"
        logger.debug("task_dispatch: provider enumeration failed", exc_info=True)
        return []
    matches = [
        row for row in rows
        if is_provider_servable(row) and model_id in curated_model_ids(row)
    ]
    matches.sort(key=lambda row: ((row.get("name") or ""), str(row.get("id") or "")))
    return matches


def _model_family(model_id: Optional[str]) -> Optional[str]:
    """The Claude model FAMILY a slug belongs to, or None.

    :func:`app.core.model_registry.get_model_family` is the app's family
    vocabulary (haiku/sonnet/opus/fable) and answers for every preset alias and
    every legacy full id it maps. A provider's curated list, though, carries
    real dated API ids the registry has never heard of
    ("claude-opus-4-20250514"), so an unresolved slug is additionally scanned
    for one of those same family tokens. The vocabulary still comes from the
    registry — this only widens what can be matched against it.
    """
    from app.core.model_registry import MODELS, get_model_family

    family = get_model_family(model_id)
    if family is not None:
        return family
    lowered = (model_id or "").lower()
    if not lowered:
        return None
    for option in MODELS:
        if option.family in lowered:
            return option.family
    return None


def _servable_providers_curating_family(family: str) -> List[Tuple[Dict[str, Any], str]]:
    """``(provider row, curated model id)`` pairs serving ``family``.

    Same admission rules and same ``(name, id)`` ordering as
    :func:`_servable_providers_curating`; the model id is the FIRST curated id
    on that row whose family matches, so the pick is stable per provider too.
    """
    from app.core.environment import is_provider_servable
    from app.core.provider_agent import curated_model_ids
    from app.db import database

    try:
        rows = database.get_enabled_model_providers()
    except Exception:  # noqa: BLE001 — a lookup failure reads as "nobody serves it"
        logger.debug("task_dispatch: provider enumeration failed", exc_info=True)
        return []
    rows.sort(key=lambda row: ((row.get("name") or ""), str(row.get("id") or "")))
    out: List[Tuple[Dict[str, Any], str]] = []
    for row in rows:
        if not is_provider_servable(row):
            continue
        for model_id in curated_model_ids(row):
            if _model_family(model_id) == family:
                out.append((row, model_id))
                break
    return out


def _example_model_slugs(limit: int = 10) -> List[str]:
    """A few real, dispatchable model ids to show alongside a rejection."""
    from app.core.environment import is_provider_servable
    from app.core.provider_agent import curated_model_ids
    from app.db import database

    out: List[str] = []
    try:
        rows = database.get_enabled_model_providers()
    except Exception:  # noqa: BLE001 — examples are a nicety, never a failure
        return out
    rows.sort(key=lambda row: ((row.get("name") or ""), str(row.get("id") or "")))
    for row in rows:
        if not is_provider_servable(row):
            continue
        for model_id in curated_model_ids(row):
            if model_id not in out:
                out.append(model_id)
            if len(out) >= limit:
                return out
    return out


def _prefer_caller(
    rows: List[Dict[str, Any]], caller_provider_id: Optional[str]
) -> Dict[str, Any]:
    """The caller's own provider row when it is among ``rows``, else the first.

    ``rows`` arrives sorted by ``(name, id)``, so the fallback is deterministic:
    the same slug always resolves to the same provider.
    """
    return next(
        (row for row in rows if str(row.get("id")) == str(caller_provider_id or "")),
        rows[0],
    )


def _route_off_claude_sub(
    model_id: str, caller_provider_id: Optional[str]
) -> ModelBinding:
    """Rule 2's preference chain for a Claude model on a non-Claude-sub caller.

    Exact curated slug → same model FAMILY → the delegated CLI leg anyway. The
    last branch is a genuine fallback, not a failure: the delegated worker does
    run, so it is spawned with a note rather than refused. NEVER called when the
    caller is itself on the Claude-sub leg — that pairing is left untouched.
    """
    exact = _servable_providers_curating(model_id)
    if exact:
        chosen = _prefer_caller(exact, caller_provider_id)
        chosen_id = str(chosen.get("id"))
        label = chosen.get("name") or chosen_id
        return ModelBinding(
            provider_id=chosen_id,
            model_id=model_id,
            note=(
                f"The target runs on the Claude subscription, which this session "
                f"is not on, so its model '{model_id}' was routed to provider "
                f"'{label}', which curates it."
            ),
        )

    family = _model_family(model_id)
    if family:
        pairs = _servable_providers_curating_family(family)
        if pairs:
            chosen, chosen_model = next(
                (
                    (row, mid) for row, mid in pairs
                    if str(row.get("id")) == str(caller_provider_id or "")
                ),
                pairs[0],
            )
            chosen_id = str(chosen.get("id"))
            label = chosen.get("name") or chosen_id
            return ModelBinding(
                provider_id=chosen_id,
                model_id=chosen_model,
                note=(
                    f"The target runs on the Claude subscription, which this "
                    f"session is not on, so its model '{model_id}' was matched at "
                    f"family level to '{chosen_model}' on provider '{label}'."
                ),
            )

    return ModelBinding(
        provider_id=None,
        model_id=model_id,
        note=(
            f"The worker ran on the Claude subscription (delegated CLI) because "
            f"no configured provider curates '{model_id}'"
            + (f" or anything in the {family} family." if family else ".")
        ),
    )


def resolve_dispatch_binding(
    *,
    target_provider_id: Optional[str],
    target_model: Optional[str],
    model_override: Optional[str],
    caller_provider_id: Optional[str],
) -> ModelBinding:
    """Decide the leg a worker runs on. Implements the rule table above.

    ``target_provider_id``/``target_model`` describe the dispatch TARGET's own
    binding (for a file-defined agent that is the caller's profile, which the
    definition borrows). ``caller_provider_id`` is the calling session's binding
    — ``None`` means the caller is itself on the Claude-subscription leg, which
    is the only thing rule 2 keys on.
    """
    from app.core.provider_agent import validate_provider_model
    from app.core.query_options import is_native_claude_model

    override = (model_override or "").strip() or None
    caller_on_claude_sub = not caller_provider_id

    # ── Rule 1/2: no override — the target's own binding, mostly untouched ──
    if not override:
        if target_provider_id:
            # Rule 1: a native-leg target runs exactly as configured, whatever
            # the caller is bound to. model_id stays None = inherit the profile.
            return ModelBinding(provider_id=target_provider_id)
        # Provider-less target = the Claude-subscription leg.
        if caller_on_claude_sub or not (target_model or "").strip():
            # Rule 2 no-op: same leg on both ends (or nothing to re-route).
            return ModelBinding(provider_id=None)
        return _route_off_claude_sub(str(target_model).strip(), caller_provider_id)

    # ── Rule 3: an explicit override — the SLUG picks the leg ───────────────
    # (a) the target's own provider already curates it.
    if target_provider_id and validate_provider_model(target_provider_id, override).ok:
        return ModelBinding(provider_id=target_provider_id, model_id=override)

    # (b) a Claude slug asked of the Claude-sub target: rule 2 decides, so a
    #     non-Claude-sub caller prefers an API provider over the CLI leg.
    if not target_provider_id and is_native_claude_model(override):
        if caller_on_claude_sub:
            return ModelBinding(provider_id=None, model_id=override)
        return _route_off_claude_sub(override, caller_provider_id)

    # (c) any configured, servable provider curating the exact slug.
    candidates = _servable_providers_curating(override)
    if candidates:
        chosen = _prefer_caller(candidates, caller_provider_id)
        chosen_id = str(chosen.get("id"))
        label = chosen.get("name") or chosen_id
        note = None
        if chosen_id != str(target_provider_id or ""):
            note = (
                f"Model '{override}' is not served by the target agent's own "
                f"binding, so the worker runs on provider '{label}'"
                + (
                    f" (one of {len(candidates)} providers curating it)."
                    if len(candidates) > 1
                    else "."
                )
            )
        return ModelBinding(provider_id=chosen_id, model_id=override, note=note)

    # (d) a Claude-family slug nobody curates — rule 2's chain again. On a
    #     Claude-sub caller this is the delegated leg, which is what
    #     ``harness_adapter._decide_provider_kind`` would have sniffed anyway;
    #     deciding it here makes it a binding rather than a guess.
    if is_native_claude_model(override):
        if caller_on_claude_sub:
            return ModelBinding(provider_id=None, model_id=override)
        return _route_off_claude_sub(override, caller_provider_id)

    # (e) nothing can serve it — synchronous failure, nothing spawned.
    default_label = (target_model or "").strip() or "its configured default"
    error = (
        f"Model '{override}' is not available on any configured provider. "
        f"The target agent's default is '{default_label}' — omit 'model' to use it."
    )
    examples = _example_model_slugs()
    if examples:
        error += " Valid model ids include: " + ", ".join(examples) + "."
    return ModelBinding(error=error)


def _credential_gap(provider_id: Optional[str]) -> Optional[str]:
    """A ready-to-return error when the resolved leg has no credentials.

    Fix-3 pre-flight: a background dispatch returns ``ok: true`` the moment the
    worker registers, so a worker that dies at provider connect (the
    ``ProviderCredentialError`` raised by
    :func:`app.core.provider.fluid_credentials.resolve_provider_credentials`)
    used to leave the caller with a success and silence. Checking the same
    *presence* condition here — connection-free, no decryption, mirroring
    ``db.count_configured_model_providers``'s definition of "usable" — turns it
    into a synchronous refusal.

    Scope, deliberate: PROVIDER-ROW legs only. A provider-less (delegated
    Claude-subscription) worker is the app's default path and its credentials
    live in the claude CLI's own config dir plus the account-failover pool; a
    filesystem probe here could refuse a perfectly working install, so that leg
    is left to the runtime. Every lookup failure also fails OPEN — this gate may
    only ever turn a certain failure into a clear message, never invent one.
    """
    if not provider_id:
        return None
    from app.db import database

    try:
        row = database.get_model_provider(provider_id, include_token=True)
    except Exception:  # noqa: BLE001 — fail open
        logger.debug(
            "task_dispatch: credential pre-flight lookup failed for %s",
            provider_id, exc_info=True,
        )
        return None
    if not row:
        return (
            f"Model provider '{provider_id}' no longer exists, so the worker "
            "would have nothing to connect to."
        )
    if not row.get("enabled"):
        return (
            f"Model provider '{row.get('name') or provider_id}' is disabled, so "
            "the worker would fail at connect. Re-enable it or pick another model."
        )
    label = row.get("name") or provider_id
    has_api_key = bool(str(row.get("auth_token") or "").strip())
    has_codex = bool(str(row.get("codex_access_token") or "").strip())
    if not has_api_key and not has_codex:
        return (
            f"Model provider '{label}' has no credentials configured, so the "
            "worker would fail at connect. Add an API key (or connect its "
            "ChatGPT account) under Settings → Model Providers."
        )
    if not has_api_key and has_codex and row.get("needs_reauth"):
        return (
            f"The ChatGPT account for model provider '{label}' needs to be "
            "reconnected before it can run a worker."
        )
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_task_tool(
    args: Dict[str, Any],
    *,
    caller_session_id: str,
    caller_profile_id: Optional[str] = None,
    launch_tool_use_id: Optional[str] = None,
    agent_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle one ``Task`` tool call. Never raises — returns a payload dict
    with ``ok`` plus either a report/worker id or an ``error``.

    ``agent_registry`` is the native leg's ``ctx.extra["agent_registry"]`` —
    file/plugin agent definitions fluid discovered on disk. It is consulted only
    when the name matches no app agent (host wins), and the resulting worker
    borrows the caller's profile with the definition's body as its system
    prompt (see the module docstring).
    """
    from app.core import provider_agent
    from app.core.config import settings

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "Task requires a prompt."}
    mode = (args.get("mode") or "sync").strip().lower()
    if mode not in ("sync", "background"):
        return {"ok": False, "error": f"Unknown mode '{mode}' (sync|background)."}

    ref = (args.get("agent") or "").strip()
    target, err = resolve_target_agent(ref)
    # Fallback ONLY after the app's own agents had their say (CE6 host-wins).
    definition = resolve_registry_agent(ref, agent_registry) if err else None
    if err and definition is None:
        return {"ok": False, "error": err}

    caller_row = _session_row(caller_session_id) or {}
    caller_pid = caller_profile_id or caller_row.get("profile_id")

    if definition is not None:
        target = None
        target_name = str(_def_field(definition, "name", ref) or ref)
        if not caller_pid:
            return {
                "ok": False,
                "error": (
                    f"Agent '{target_name}' is defined in a file, so its worker "
                    "runs on the calling session's profile — but this session "
                    "has no profile to inherit. Open the session with a profile "
                    "and retry."
                ),
            }
    else:
        assert target is not None
        target_name = str(target.get("name") or target["id"])

    # NO invoke allowlist here, by design (2026-08-17 ruling, superseding A10):
    # ``enabled_agents``/``agent_invoke_mode`` only decide what the caller's
    # CONTEXT advertises (app.core.agent_context), never what it may run. An
    # undeclared agent the model names anyway still dispatches.

    # Nesting depth + cycles (A9).
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    _, root_session_id, caller_depth = caller_chain(caller_session_id)
    if caller_depth + 1 > max_depth:
        return {
            "ok": False,
            "error": f"Nesting limit reached ({max_depth} levels of agent-invokes-agent).",
        }
    # Cycles (A9) are a PROFILE-id property; a file agent has no profile of its
    # own (it borrows the caller's), so the check would refuse every dispatch.
    if target is not None:
        active_agents = _chain_agent_ids(caller_session_id)
        if target["id"] in active_agents:
            return {
                "ok": False,
                "error": (
                    f"Cycle refused: agent '{target['id']}' is already active in "
                    "this dispatch chain."
                ),
            }

    # Worker budget (A9), counted across the whole root tree.
    max_workers = int(getattr(settings, "task_tool_max_workers", 8) or 8)
    if _live_worker_count_for_root(root_session_id) >= max_workers:
        return {
            "ok": False,
            "error": (
                f"Worker budget reached ({max_workers} live workers for this "
                "session). Wait for running workers or stop them."
            ),
        }

    model_override = (args.get("model") or "").strip() or None
    label = (args.get("label") or "").strip() or f"Task · {target_name}"
    #: Non-fatal deviations reported back to the model with the result.
    notes: List[str] = []

    caller_provider_id = _caller_provider_id(caller_row, caller_pid)

    if definition is not None:
        # The worker rides the CALLER's profile and provider binding; only the
        # system prompt (and, when resolvable, the model) come from the file.
        profile_id = str(caller_pid)
        provider_id = caller_provider_id
        # The definition's own ``model:`` is the target's configured model here
        # (the borrowed profile has no opinion about it); an explicit override
        # still wins, exactly as it does for an app agent.
        wanted = str(_def_field(definition, "model", "") or "").strip() or None
        if model_override or wanted:
            binding = resolve_dispatch_binding(
                target_provider_id=provider_id,
                target_model=wanted,
                model_override=model_override or wanted,
                caller_provider_id=caller_provider_id,
            )
            if binding.error:
                # A file agent must still spawn (ruling CE6): an unservable
                # model degrades to the session's own, never to a refusal.
                notes.append(
                    f"The definition asks for model '{model_override or wanted}', "
                    "which no configured provider can serve — the worker inherits "
                    "the session's model instead."
                )
                model_override = None
            else:
                provider_id = binding.provider_id
                model_override = binding.model_id or model_override or wanted
                if binding.note:
                    notes.append(binding.note)
        tool_allowlist = _definition_tool_allowlist(definition)
        if tool_allowlist:
            notes.append(
                "The worker's tool surface is restricted to the definition's "
                "tools list: " + ", ".join(tool_allowlist) + "."
            )
        system_prompt_append = _file_agent_prompt(definition)
        logger.info(
            "task_dispatch: dispatching file-defined agent %r (scope=%s, path=%s) "
            "on caller profile %s",
            target_name, _def_field(definition, "scope", "?"),
            _def_field(definition, "path", "?"), profile_id,
        )
    else:
        assert target is not None
        admission_error = _validate_target(target)
        if admission_error:
            return {"ok": False, "error": admission_error}
        profile_id = target["id"]
        target_config = target.get("config") or {}
        binding = resolve_dispatch_binding(
            target_provider_id=target_config.get("provider") or None,
            target_model=target_config.get("model") or None,
            model_override=model_override,
            caller_provider_id=caller_provider_id,
        )
        if binding.error:
            return {"ok": False, "error": binding.error}
        provider_id = binding.provider_id
        model_override = binding.model_id
        if binding.note:
            notes.append(binding.note)
        system_prompt_append = None
        # An app agent carries its own profile (and with it its own
        # allowed/disallowed tool config) — nothing to override per-dispatch.
        tool_allowlist = None

    # Fix-3 pre-flight: refuse SYNCHRONOUSLY when the resolved leg has no
    # credentials, rather than returning ok:true and letting a background worker
    # die at connect half a second later.
    credential_error = _credential_gap(provider_id)
    if credential_error:
        return {"ok": False, "error": credential_error}

    kwargs: Dict[str, Any] = dict(
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        profile_id=profile_id,
        provider_id=provider_id,
        model_id=model_override,
        prompt=prompt,
        label=label,
        # The model's Task tool_use_id (fluid native leg) — lets the worker
        # card absorb the launch tool_use block instead of rendering both.
        launch_tool_use_id=launch_tool_use_id,
    )
    if system_prompt_append:
        kwargs["system_prompt_append"] = system_prompt_append
    if tool_allowlist:
        kwargs["tool_allowlist"] = tool_allowlist

    if mode == "background":
        task = asyncio.create_task(
            provider_agent.dispatch_worker(background=True, **kwargs)
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        # The worker session id is minted inside dispatch_worker; wait briefly
        # for registration so the caller gets a pollable id.
        worker_id = await _await_worker_registration(root_session_id, label)
        return {
            "ok": True,
            "mode": "background",
            "agent": target["id"] if target is not None else target_name,
            "worker_session_id": worker_id,
            "note": " ".join([
                "Worker dispatched in the background. Poll with TaskList/"
                "TaskOutput (task id = worker session id).",
                *notes,
            ]),
        }

    outcome = await provider_agent.dispatch_worker(**kwargs)
    payload: Dict[str, Any] = {
        "ok": outcome.ok,
        "mode": "sync",
        "agent": target["id"] if target is not None else target_name,
        "worker_session_id": outcome.session_id,
    }
    if notes:
        payload["note"] = " ".join(notes)
    if outcome.ok:
        payload["report"] = outcome.report or "(no report)"
        if outcome.truncated:
            payload["truncated"] = True
    else:
        payload["error"] = outcome.error or "Worker failed."
    return payload


async def _await_worker_registration(
    root_session_id: str, label: str, timeout: float = 5.0
) -> Optional[str]:
    """Best-effort: return the just-dispatched background worker's session id
    once the supervisor sees it (labels match)."""
    from app.core import provider_agent_supervisor as sup

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for record in reversed(sup.live_workers(root_session_id)):
            if (record.label or "").strip() == label.strip():
                return record.worker_session_id
        await asyncio.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Tool-surface adapters
# ---------------------------------------------------------------------------

def make_fluid_dispatcher(
    session_id: "str | Callable[[], Optional[str]]",
    profile_id: Optional[str] = None,
):
    """The ``ctx.extra['task_dispatch']`` callable for fluid's bare Task builtin
    (native legs).

    ``session_id`` is the caller's **app** session id (``sessions.id``) — the
    row Task dispatch walks for lineage, budget, and worker parenting. It may
    be a plain string or a zero-argument callable resolved at CALL time.

    Late binding is the point: the native leg builds its ``ToolCtx`` inside
    ``HarnessAdapter.connect()``, and a brand-new session has no id to bind
    there yet. The ctx (and therefore this closure) then lives for the whole
    pooled life of the engine, so a connect-time snapshot would stay empty
    forever and every Task call would report the tool unavailable.

    ``ctx.session_id`` is deliberately NOT used as a fallback: on a native leg
    it holds the *fluid* session id (``ses_...``), which is not a
    ``sessions.id`` — dispatching against it would parent the worker onto a
    session row that does not exist.
    """

    async def _dispatch(input: Dict[str, Any], ctx: Any) -> str:
        caller_session_id: Any = session_id
        if callable(caller_session_id):
            try:
                caller_session_id = caller_session_id()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "task_dispatch: caller session-id supplier raised; Task "
                    "cannot be dispatched for this call",
                    exc_info=True,
                )
                caller_session_id = None
        if not caller_session_id:
            logger.warning(
                "task_dispatch: no app session id resolved at dispatch time — "
                "refusing to dispatch a parentless worker"
            )
            return json.dumps({
                "ok": False,
                "error": (
                    "Task dispatch could not resolve the calling session — the "
                    "worker would have no parent session to report back to. "
                    "This is a host wiring problem, not a problem with the "
                    "request; retry once the session is fully registered."
                ),
            })
        # Stamped by the engine for the duration of this dispatch (NativeLoop
        # sets ctx.extra["tool_use_id"] per tool call; dispatches are
        # sequential). Absent on the delegated leg's bridged mcp tool.
        launch_tool_use_id = None
        agent_registry = None
        try:
            extra = getattr(ctx, "extra", None) or {}
            candidate = extra.get("tool_use_id")
            if isinstance(candidate, str) and candidate:
                launch_tool_use_id = candidate
            # File/plugin agent definitions fluid discovered for this session
            # (ruling CE6). Read at CALL time, not closure time: NativeLoop
            # populates it while constructing itself, which happens AFTER this
            # dispatcher is built and injected into the very same ctx.
            registry = extra.get("agent_registry")
            if isinstance(registry, dict) and registry:
                agent_registry = registry
        except Exception:  # noqa: BLE001 — display-only nicety + optional seam
            launch_tool_use_id = None
        payload = await dispatch_task_tool(
            input,
            caller_session_id=str(caller_session_id),
            caller_profile_id=profile_id,
            launch_tool_use_id=launch_tool_use_id,
            agent_registry=agent_registry,
        )
        return json.dumps(payload, default=str)

    return _dispatch
