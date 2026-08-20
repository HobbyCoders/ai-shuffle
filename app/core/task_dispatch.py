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
  ``mcp__builtins__TaskList`` / ``mcp__builtins__TaskOutput`` — those are the
  names both legs actually advertise; background workers survive the caller's
  turn end);
* LEG RESOLUTION — current chats stay on the provider/model scope selected at
  chat start; target agents contribute persona and tools only. Imported legacy
  sessions without ``launched_model`` retain the 2026-08-17 compatibility
  table above :class:`ModelBinding`. Every resolved leg is credential-checked
  before dispatch so a doomed worker fails synchronously.

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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover — annotation-only; every DB import here is
    # deliberately lazy (see the function bodies), so the row types ride the
    # string annotations ``from __future__ import annotations`` already gives us.
    from app.db.model_providers import ModelProviderRow
    from app.db.profiles import ProfileRow
    from app.db.sessions import SessionRow

logger = logging.getLogger(__name__)

#: Hard ceiling on parent-chain walks — above max depth anyway.
_CHAIN_LIMIT = 12

#: What the Task tool's ``model`` property tells the model, on BOTH legs (the
#: native leg's bare builtin via ``harness_adapter._amend_native_task_spec``,
#: the delegated leg's ``mcp__builtins__Task`` via ``app.mcp.builtin_tools``).
#:
#: The schema now carries a provider-scoped enum assembled by
#: :func:`provider_scoped_task_models`.  The selected chat model is always first;
#: every additional value comes from that exact provider row.  This keeps an
#: override mechanically constrained without letting an agent-builder profile
#: inject a model from another provider.
TASK_MODEL_OVERRIDE_DESCRIPTION = (
    "Optional model override — normally OMIT it; omission uses the model selected "
    "when this chat started. If supplied, choose only from this schema's enum: it "
    "contains the selected chat model plus configured models from this chat's "
    "provider, never the target agent's builder model or an unrelated provider model."
)

#: Background dispatch tasks, kept referenced so asyncio never GCs them.
_BACKGROUND_TASKS: set = set()


def provider_scoped_task_models(
    provider_id: Optional[str], selected_model: Optional[str]
) -> Tuple[str, ...]:
    """Return the Task override roster for one running chat.

    The running session's provider binding is authoritative.  The chat's
    selected model is included even when an older provider row has not curated
    it yet; all other entries come solely from that same row's ``models_json``.
    Ordering is stable and duplicates are removed without consulting the target
    agent/profile, which is the boundary that prevents cross-provider leakage.
    """
    ordered: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            value = value.strip()
            if value and value not in ordered:
                ordered.append(value)

    add(selected_model)
    if not provider_id:
        return tuple(ordered)
    try:
        from app.db import database
        from app.core.provider_agent import curated_model_ids

        row = database.get_model_provider(str(provider_id), include_token=False)
        for model_id in curated_model_ids(row):
            add(model_id)
    except Exception:  # noqa: BLE001 — schema construction must never block chat
        logger.warning(
            "Task model roster lookup failed for provider %s", provider_id,
            exc_info=True,
        )
    return tuple(ordered)


def task_model_property_schema(
    provider_id: Optional[str], selected_model: Optional[str]
) -> Dict[str, Any]:
    """JSON-schema property shared by native and delegated Task tools."""
    schema: Dict[str, Any] = {
        "type": "string",
        "description": TASK_MODEL_OVERRIDE_DESCRIPTION,
    }
    models = provider_scoped_task_models(provider_id, selected_model)
    if models:
        schema["enum"] = list(models)
    return schema


# ---------------------------------------------------------------------------
# Chain / depth helpers
# ---------------------------------------------------------------------------

def _session_row(session_id: str) -> Optional[SessionRow]:
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


def caller_chain(session_id: str) -> Tuple[List[SessionRow], str, int]:
    """Walk the parent chain upward from ``session_id``.

    Returns ``(worker_ancestor_rows, root_session_id, depth)`` where ``depth``
    is the number of worker sessions in the chain INCLUDING ``session_id``
    itself (0 for a top-level human session), and ``root_session_id`` is the
    top non-worker session (falls back to the highest reachable ancestor).
    """
    rows: List[SessionRow] = []
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

def resolve_target_agent(ref: str) -> Tuple[Optional[ProfileRow], Optional[str]]:
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
    tools: Tuple[Any, ...] = tuple(_def_field(definition, "tools", ()) or ())

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


def _caller_provider_id(
    caller_row: Optional[SessionRow], caller_profile_id: Optional[str]
) -> Optional[str]:
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


def _validate_target(agent: ProfileRow) -> Optional[str]:
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
# Legacy worker-leg resolution (ruling 2026-08-17, as refined)
# ---------------------------------------------------------------------------
#
# Current chats persist ``sessions.launched_model`` and bypass this table in
# ``dispatch_task_tool``: their workers stay on the chat's provider and either
# its selected model or another model curated by that provider. The table below
# remains only for imported/legacy sessions that have no durable launched-model
# binding, so those sessions keep their historical behavior instead of failing.
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
# 3. AN EXPLICIT ``model`` OVERRIDE IS THE CALLER'S OWN PROVIDER ONLY
#    (2026-08-17 ruling, superseding the earlier free cross-provider binding).
#    Agents run at THEIR selected model; a caller does not get to shop for
#    models it merely knows the name of. So an override is ADMITTED only when:
#
#      * the CALLER is bound to a provider row that curates the slug
#        (``validate_provider_model`` on ``caller_provider_id``), or
#      * the caller has NO row (it is on the Claude subscription) and the slug
#        is a Claude-family one — then the existing claude-sub rules decide.
#
#    An override that fails that gate is DROPPED with a note and the dispatch
#    proceeds on the TARGET's own configured provider+model (i.e. exactly the
#    no-override resolution above). A bad override NEVER fails a dispatch.
#
#    Once admitted, the leg is picked as before: (a) the target's own provider
#    when it curates the slug too, (b) a Claude slug asked of a Claude-sub
#    target, (c) any configured servable provider curating the exact slug (the
#    caller's own preferred), (d) rule 2's chain for a Claude-family slug.
#    Rationale (live incident): a worker overrode a subscription-bound agent
#    onto a metered API row it happened to be able to name.
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
    set the dispatch fails synchronously and nothing is spawned — no override
    path sets it any more (rule 3 drops instead of failing), but the field
    stays so a future hard refusal has somewhere to live.

    ``rule`` names the branch that produced this binding ("1", "2-exact",
    "2-family", "2-cli", "3a".."3d", "drop"). Display/diagnostics only — it is
    what the per-dispatch INFO log reports.
    """

    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None
    rule: Optional[str] = None


def _is_subscription_row(row: ModelProviderRow) -> bool:
    """Whether a ``model_providers`` row authenticates as a SUBSCRIPTION.

    Third mirror of the same two-field test (``app.core.model_registry.
    _is_subscription_row``, ``app.core.provider.fluid_credentials.
    _is_codex_subscription`` / ``_is_grok_subscription``): ChatGPT
    (``chatgpt``) or SuperGrok (``grok``) via ``provider_type`` or
    ``auth_mode``. Everything else is metered API-key auth.
    """
    row = row or {}
    return (
        row.get("provider_type") in ("chatgpt", "grok")
        or row.get("auth_mode") in ("chatgpt", "grok")
    )


def _subscription_first(rows: List[ModelProviderRow]) -> None:
    """Re-order ``rows`` IN PLACE so subscription-auth rows lead.

    Applied AFTER the ``(name, id)`` sort and stable, so the tie-break order
    inside each group is untouched: the pick stays deterministic, it just stops
    reaching for a metered API row while the user's own subscription can serve
    the same model. This is what every ``rows[0]`` fallback below inherits.
    """
    rows.sort(key=lambda row: 0 if _is_subscription_row(row) else 1)


def _servable_providers_curating(model_id: str) -> List[ModelProviderRow]:
    """Enabled + servable provider rows whose curated list holds ``model_id``.

    Reuses the two existing admission helpers rather than re-deriving them:
    :func:`app.core.environment.is_provider_servable` (the cross-provider WORKER
    admission rule) and :func:`app.core.provider_agent.curated_model_ids` (the
    one defensive parser for ``models_json``). Sorted by ``(name, id)`` so a
    slug curated by several providers always resolves to the same one, then
    subscription rows are floated to the front (see :func:`_subscription_first`).
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
    _subscription_first(matches)
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


def _servable_providers_curating_family(family: str) -> List[Tuple[ModelProviderRow, str]]:
    """``(provider row, curated model id)`` pairs serving ``family``.

    Same admission rules and same ``(name, id)`` + subscription-first ordering
    as :func:`_servable_providers_curating`; the model id is the FIRST curated
    id on that row whose family matches, so the pick is stable per provider too.
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
    _subscription_first(rows)
    out: List[Tuple[ModelProviderRow, str]] = []
    for row in rows:
        if not is_provider_servable(row):
            continue
        for model_id in curated_model_ids(row):
            if _model_family(model_id) == family:
                out.append((row, model_id))
                break
    return out


def _prefer_caller(
    rows: List[ModelProviderRow], caller_provider_id: Optional[str]
) -> ModelProviderRow:
    """The caller's own provider row when it is among ``rows``, else the first.

    ``rows`` arrives sorted by ``(name, id)`` with subscription-auth rows
    floated to the front (:func:`_subscription_first`), so the fallback is both
    deterministic AND cheapest-first: the same slug always resolves to the same
    provider, and a user's own subscription is never passed over for a metered
    API row that curates the same model.
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
            rule="2-exact",
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
                rule="2-family",
            )

    return ModelBinding(
        provider_id=None,
        model_id=model_id,
        note=(
            f"The worker ran on the Claude subscription (delegated CLI) because "
            f"no configured provider curates '{model_id}'"
            + (f" or anything in the {family} family." if family else ".")
        ),
        rule="2-cli",
    )


def resolve_dispatch_binding(
    *,
    target_provider_id: Optional[str],
    target_model: Optional[str],
    model_override: Optional[str],
    caller_provider_id: Optional[str],
    target_name: Optional[str] = None,
) -> ModelBinding:
    """Decide the leg a worker runs on. Implements the rule table above.

    ``target_provider_id``/``target_model`` describe the dispatch TARGET's own
    binding (for a file-defined agent that is the caller's profile, which the
    definition borrows). ``caller_provider_id`` is the calling session's binding
    — ``None`` means the caller is itself on the Claude-subscription leg, which
    is what rule 2 keys on AND what decides whether an override is admissible at
    all (rule 3). ``target_name`` is display-only: it names the agent in the
    dropped-override note.
    """
    from app.core.provider_agent import validate_provider_model
    from app.core.query_options import is_native_claude_model

    override = (model_override or "").strip() or None
    caller_on_claude_sub = not caller_provider_id

    def _target_binding() -> ModelBinding:
        """Rules 1/2 — the target's own binding, mostly untouched."""
        if target_provider_id:
            # Rule 1: a native-leg target runs exactly as configured, whatever
            # the caller is bound to. model_id stays None = inherit the profile.
            return ModelBinding(provider_id=target_provider_id, rule="1")
        # Provider-less target = the Claude-subscription leg.
        if caller_on_claude_sub or not (target_model or "").strip():
            # Rule 2 no-op: same leg on both ends (or nothing to re-route).
            return ModelBinding(provider_id=None, rule="1")
        return _route_off_claude_sub(str(target_model).strip(), caller_provider_id)

    if not override:
        return _target_binding()

    # ── Rule 3 admission: the override must be the CALLER's own to give ─────
    # An agent runs at ITS selected model; the caller may only redirect it onto
    # something its OWN provider curates. A Claude-sub caller has no row, so its
    # only currency is the Claude family (the claude-sub rules below decide the
    # rest). Anything else is dropped — never a refusal.
    if caller_on_claude_sub:
        admitted = is_native_claude_model(override)
    else:
        admitted = validate_provider_model(str(caller_provider_id), override).ok
    if not admitted:
        base = _target_binding()
        agent_label = (target_name or "").strip() or "the agent"
        model_label = (target_model or "").strip() or "model"
        drop_note = (
            f"Model override '{override}' isn't curated on your provider; "
            f"ran {agent_label} on its configured {model_label}."
        )
        return ModelBinding(
            provider_id=base.provider_id,
            model_id=base.model_id,
            note=" ".join(p for p in (drop_note, base.note) if p),
            rule="drop",
        )

    # ── Rule 3, admitted: the slug picks the leg among what the caller may use ─
    # (a) the target's own provider already curates it (and so does the caller's,
    #     or the caller is the claude-sub case — the admission gate above).
    if target_provider_id and validate_provider_model(target_provider_id, override).ok:
        return ModelBinding(
            provider_id=target_provider_id, model_id=override, rule="3a",
        )

    # (b) a Claude slug asked of the Claude-sub target: rule 2 decides, so a
    #     non-Claude-sub caller prefers an API provider over the CLI leg.
    if not target_provider_id and is_native_claude_model(override):
        if caller_on_claude_sub:
            return ModelBinding(provider_id=None, model_id=override, rule="3b")
        return _route_off_claude_sub(override, caller_provider_id)

    # (c) any configured, servable provider curating the exact slug.
    candidates = _servable_providers_curating(override)
    if candidates:
        chosen = _prefer_caller(candidates, caller_provider_id)
        chosen_id = str(chosen.get("id"))
        label = chosen.get("name") or chosen_id
        # The note used to compare the chosen row against the TARGET only, so
        # "your own subscription was passed over" was unreportable — the very
        # thing the live incident needed said. Compare against BOTH ends.
        passed_over: List[str] = []
        if chosen_id != str(target_provider_id or ""):
            passed_over.append("the target agent's own binding")
        if chosen_id != str(caller_provider_id or ""):
            passed_over.append(
                "this session's own provider" if caller_provider_id
                else "the Claude subscription this session runs on"
            )
        note = None
        if passed_over:
            note = (
                f"Model '{override}' is not served by "
                + " or ".join(passed_over)
                + f", so the worker runs on provider '{label}'"
                + (
                    f" (one of {len(candidates)} providers curating it)."
                    if len(candidates) > 1
                    else "."
                )
            )
        return ModelBinding(
            provider_id=chosen_id, model_id=override, note=note, rule="3c",
        )

    # (d) a Claude-family slug nobody curates — rule 2's chain again. On a
    #     Claude-sub caller this is the delegated leg, which is what
    #     ``harness_adapter._decide_provider_kind`` would have sniffed anyway;
    #     deciding it here makes it a binding rather than a guess.
    if is_native_claude_model(override):
        if caller_on_claude_sub:
            return ModelBinding(provider_id=None, model_id=override, rule="3d")
        return _route_off_claude_sub(override, caller_provider_id)

    # (e) admitted but unservable (the caller's own row curates it yet cannot be
    #     served as a worker leg — e.g. a ``custom`` row). Still never fatal.
    base = _target_binding()
    agent_label = (target_name or "").strip() or "the agent"
    model_label = (target_model or "").strip() or "model"
    return ModelBinding(
        provider_id=base.provider_id,
        model_id=base.model_id,
        note=" ".join(p for p in (
            f"Model override '{override}' cannot be served by any provider that "
            f"can run a worker; ran {agent_label} on its configured {model_label}.",
            base.note,
        ) if p),
        rule="drop",
    )


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

    caller_row = _session_row(caller_session_id)
    caller_pid = caller_profile_id or (caller_row or {}).get("profile_id")

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
    #: Which branch of the rule table decided the leg — for the dispatch log.
    binding_rule = "none"

    caller_provider_id = _caller_provider_id(caller_row, caller_pid)
    caller_model = str((caller_row or {}).get("launched_model") or "").strip() or None
    session_bound = caller_model is not None

    if session_bound:
        # A concrete chat's provider/model binding is authoritative. Target
        # profiles contribute persona and tools, not a hidden provider/model
        # switch from the agent builder. Recheck the provider roster here
        # because malformed/direct tool calls can bypass JSON Schema.
        roster = provider_scoped_task_models(caller_provider_id, caller_model)
        requested_model = model_override or caller_model
        if requested_model not in roster:
            notes.append(
                f"Model '{requested_model}' is not available on this chat's "
                f"provider; the worker uses the chat-selected model '{caller_model}'."
            )
            requested_model = caller_model
        provider_id = caller_provider_id
        model_override = requested_model
        binding_rule = "session"

    if definition is not None:
        # The worker rides the CALLER's profile and provider binding; only the
        # system prompt (and, when resolvable, the model) come from the file.
        profile_id = str(caller_pid)
        provider_id = caller_provider_id
        # The definition's own ``model:`` is the target's configured model here
        # (the borrowed profile has no opinion about it); an explicit override
        # still wins, exactly as it does for an app agent.
        wanted = str(_def_field(definition, "model", "") or "").strip() or None
        if session_bound:
            # File-agent model metadata cannot override a live chat binding.
            pass
        elif model_override or wanted:
            wanted_model = model_override or wanted
            binding = resolve_dispatch_binding(
                target_provider_id=provider_id,
                target_model=wanted,
                model_override=wanted_model,
                caller_provider_id=caller_provider_id,
                target_name=target_name,
            )
            binding_rule = binding.rule or "?"
            provider_id = binding.provider_id
            # NEVER fall back to the requested slug here: a DROPPED override
            # resolves to model_id=None ("inherit"), and re-applying the slug
            # would smuggle the very binding the drop rule refused.
            model_override = binding.model_id
            if binding.rule == "drop":
                # A file agent must still spawn (ruling CE6): an inadmissible
                # model degrades to the session's own, never to a refusal.
                notes.append(
                    f"Model '{wanted_model}' is not curated on this session's "
                    "provider — the worker inherits the session's model instead."
                )
            elif binding.note:
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
        profile_id = target["id"]
        target_config = target.get("config") or {}
        if not session_bound:
            # Compatibility for imported sessions that predate launched_model.
            admission_error = _validate_target(target)
            if admission_error:
                return {"ok": False, "error": admission_error}
            binding = resolve_dispatch_binding(
                target_provider_id=target_config.get("provider") or None,
                target_model=target_config.get("model") or None,
                model_override=model_override,
                caller_provider_id=caller_provider_id,
                target_name=target_name,
            )
            if binding.error:
                return {"ok": False, "error": binding.error}
            binding_rule = binding.rule or "?"
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

    # ONE line per dispatch: what the caller was on, what the worker got, which
    # branch of the rule table decided it, and whatever the caller is being told.
    logger.info(
        "task_dispatch: %s (session=%s provider=%s) -> agent %r on "
        "provider=%s model=%s [rule=%s]%s",
        mode, caller_session_id, caller_provider_id or "claude-sub",
        target_name, provider_id or "claude-sub", model_override or "inherit",
        binding_rule,
        f" note={' '.join(notes)!r}" if notes else "",
    )

    kwargs: Dict[str, Any] = dict(
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        profile_id=profile_id,
        provider_id=provider_id,
        model_id=model_override,
        prompt=prompt,
        label=label,
        # The agent's NAME — what the subagent card must show as the worker's
        # identity (the model slug is a secondary detail, ruling 2026-08-17).
        agent_name=target_name,
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
                # The polling tools ride the ``builtins`` MCP server on BOTH
                # legs — the native leg only swaps Task/ToolSearch for fluid's
                # own builtins (harness_adapter's ``skip_tools``), so the bare
                # names "TaskList"/"TaskOutput" this note used to advertise
                # exist on neither leg's tool surface.
                "Worker dispatched in the background. Poll with "
                "mcp__builtins__TaskList / mcp__builtins__TaskOutput "
                "(task id = worker session id).",
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
