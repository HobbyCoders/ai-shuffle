"""Provider-scoped model assignments and model-facing subagent discovery.

``config.provider_models`` is the ONE model binding an agent still carries: the
default model it runs on when another agent dispatches it as a Task subagent.
Task may choose another model from that provider's curated list. Top-level
chat sessions do not read an agent's model — they carry their own, picked at
creation — so the legacy ``config.provider`` / ``config.model`` pair survives
here only as a fallback for agents configured before that change.

The current caller provider chooses the map **entry**. That entry may name any
leg — a native Claude alias or a ``prov:<providerId>:<modelId>`` composite —
and a mismatch pins the worker onto the named provider. A model-authored Task
call never chooses or changes providers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ``model_providers.id`` values are ordinary row ids. This deliberately noisy
# sentinel cannot be mistaken for one of those rows and gives the native Claude
# leg a stable JSON/UI key.
NATIVE_PROVIDER_KEY = "__native__"

# The voice agent is not a provider: a voice call is funded by an OpenAI
# Realtime row, but the WORKERS it dispatches can run on any leg. This key in
# ``config.provider_models`` holds the agent's voice-mode assignment — a bare
# native model id (Claude) or a ``prov:<providerId>:<modelId>`` composite — and
# is what makes an agent dispatchable from a voice call at all. Same sentinel
# discipline as the native key: it can never collide with a provider row id.
VOICE_PROVIDER_KEY = "__voice__"


def provider_key(provider_id: str | None) -> str:
    """Return the config-map key for a provider binding."""
    return str(provider_id).strip() if provider_id else NATIVE_PROVIDER_KEY


def _load_provider_row(provider_id: str) -> Any:
    """Return a ``model_providers`` row, or ``None`` on lookup failure."""
    from app.db import database

    try:
        return database.get_model_provider(provider_id, include_token=False)
    except TypeError:
        # Test doubles and older database adapters may not accept the
        # include_token keyword.
        try:
            return database.get_model_provider(provider_id)
        except Exception:
            logger.debug("Could not load subagent provider", exc_info=True)
            return None
    except Exception:
        logger.debug("Could not load subagent provider", exc_info=True)
        return None


def _validated_provider_model(
    provider_id: str, model: str, provider: Any
) -> bool:
    """True when ``model`` is a curated, enabled assignment on ``provider``."""
    if not provider or not provider.get("enabled"):
        return False
    from app.core.provider_agent import validate_provider_model

    try:
        validation = validate_provider_model(
            provider_id,
            model,
            provider_row=provider,
            require_servable=False,
        )
    except Exception:
        logger.debug(
            "Could not validate subagent model %s on provider %s",
            model,
            provider_id,
            exc_info=True,
        )
        return False
    return bool(getattr(validation, "ok", False))


def normalized_provider_models(config: Mapping[str, Any]) -> dict[str, str]:
    """Return valid stored assignments plus the legacy top-level binding.

    The legacy fallback is intentionally provider-local. An old native-Claude
    agent becomes available on native Claude, and an old provider-bound agent
    becomes available on that provider. Neither is inferred onto other
    providers. Newer agents have no top-level model at all, so for them the
    stored map is the whole answer.
    """
    result: dict[str, str] = {}
    raw = config.get("provider_models")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            clean_key = key.strip()
            clean_value = value.strip()
            if clean_key and clean_value:
                result[clean_key] = clean_value

    legacy_model = config.get("model")
    if isinstance(legacy_model, str) and legacy_model.strip():
        result.setdefault(
            provider_key(config.get("provider")), legacy_model.strip()
        )
    return result


def model_for_provider(
    config: Mapping[str, Any], provider_id: str | None
) -> str | None:
    """Resolve an agent's subagent model on exactly ``provider_id``."""
    return normalized_provider_models(config).get(provider_key(provider_id))


@dataclass(frozen=True)
class SubagentTarget:
    """One provider-compatible Task target exposed to a model."""

    id: str
    name: str
    description: str
    model: str
    # True when the target's declared tool surface cannot write files, i.e. it
    # is an Explore-class research agent. ``task_dispatch`` dispatches these in
    # the background whatever the call asks for, so the contract must not offer
    # ``mode`` as if it were a live choice.
    background_default: bool = False
    # A target normally runs on the CALLER's provider. ``provider_pinned`` says
    # ``provider_id`` (``None`` = native Claude) is the leg the worker must run
    # on instead — voice assignments always pin, and a chat Task assignment pins
    # when its stored value names a different provider than the caller.
    provider_id: str | None = None
    provider_pinned: bool = False

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "model": self.model,
        }


def _clean_description(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    # Cut at a word boundary — a mid-word chop ("how do A…") reads as prompt
    # corruption to the model. Fall back to a hard cut only for a single
    # pathological token.
    cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:
        cut = limit - 1
    return text[:cut].rstrip() + "…"


def available_subagents(
    caller_profile_id: str | None, provider_id: str | None
) -> tuple[SubagentTarget, ...]:
    """Return the exact Task roster for a caller and its current provider.

    Availability is the intersection of the caller's optional invoke allowlist
    and agents with a valid model assignment **for** the current provider. The
    assignment itself may name another leg; those targets come back pinned so
    dispatch runs the worker there. The same function feeds Task's schema and
    its runtime admission check.
    """
    if not caller_profile_id:
        return ()

    from app.core.builtin_subagents import is_read_only_tool_surface
    from app.db import database

    try:
        caller = database.get_profile(caller_profile_id)
    except Exception:
        logger.debug("Could not load caller profile for subagent discovery", exc_info=True)
        return ()
    if not caller:
        return ()
    caller_config = caller.get("config") or {}
    raw_allowlist = caller_config.get("enabled_agents")
    # Current profiles persist the choice explicitly: ``all`` exposes every
    # provider-compatible target, while ``custom`` exposes exactly the selected
    # IDs (including an empty set). Older profiles had no mode; preserve their
    # historical empty-means-all behavior.
    invoke_mode = caller_config.get("agent_invoke_mode")
    if invoke_mode == "custom":
        allowlist = {
            str(value)
            for value in (raw_allowlist or [])
            if isinstance(value, str)
        }
    elif invoke_mode == "all":
        allowlist = None
    else:
        allowlist = (
            {str(value) for value in raw_allowlist if isinstance(value, str)}
            if isinstance(raw_allowlist, list) and raw_allowlist
            else None
        )

    # Fail closed when the CALLER's provider is missing or disabled — the parent
    # could not be running on it. A *target* assignment that names a bad
    # provider is skipped per-agent below, not treated as an empty roster.
    provider_cache: dict[str, Any] = {}
    if provider_id:
        caller_provider = _load_provider_row(provider_id)
        if not caller_provider or not caller_provider.get("enabled"):
            return ()
        provider_cache[provider_id] = caller_provider
    targets = []
    try:
        rows = database.get_all_profiles() or []
    except Exception:
        logger.debug("Could not list profiles for subagent discovery", exc_info=True)
        return ()
    caller_key = provider_id or None
    map_key = provider_key(provider_id)
    for row in rows:
        target_id = str(row.get("id") or "").strip()
        if not target_id or target_id == caller_profile_id:
            continue
        if allowlist is not None and target_id not in allowlist:
            continue

        config = row.get("config") or {}
        binding = decode_subagent_binding(
            map_key, model_for_provider(config, provider_id)
        )
        if binding is None:
            continue
        target_provider_id, model = binding

        if target_provider_id:
            if target_provider_id not in provider_cache:
                provider_cache[target_provider_id] = _load_provider_row(
                    target_provider_id
                )
            if not _validated_provider_model(
                target_provider_id, model, provider_cache[target_provider_id]
            ):
                continue

        pinned = target_provider_id != caller_key
        targets.append(
            SubagentTarget(
                id=target_id,
                name=str(row.get("name") or target_id).strip() or target_id,
                description=_clean_description(row.get("description")),
                model=model,
                # ``allowed_tools`` is the same field db.subagents surfaces as
                # ``tools`` and task_dispatch classifies on — one source, so
                # schema and runtime cannot disagree about a target's mode.
                background_default=is_read_only_tool_surface(
                    config.get("allowed_tools")
                ),
                provider_id=target_provider_id if pinned else None,
                provider_pinned=pinned,
            )
        )

    return tuple(sorted(targets, key=lambda target: (target.name.casefold(), target.id)))


def decode_subagent_binding(
    key: str, value: Any
) -> tuple[str | None, str] | None:
    """``(provider_id, model_id)`` for a stored Task assignment, else ``None``.

    A ``prov:<providerId>:<modelId>`` composite names that provider row. A bare
    Claude alias is the native leg, even when stored under a caller-provider
    key (the Agents tab lets every row pick any model). A bare non-Claude id
    under a provider key is the pre-unscoped legacy shape: that model on that
    provider. Empty or malformed reads as "not assigned".
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if not isinstance(key, str) or not key.strip():
        return None
    from app.core.query_options import decode_provider_model, is_native_claude_model

    text = value.strip()
    decoded = decode_provider_model(text)
    if decoded is not None:
        return decoded
    if text.startswith("prov:"):
        return None  # a composite with a missing half — not a native id
    clean_key = key.strip()
    if (
        clean_key in (NATIVE_PROVIDER_KEY, VOICE_PROVIDER_KEY)
        or is_native_claude_model(text)
    ):
        return None, text
    return clean_key, text


def decode_voice_binding(value: Any) -> tuple[str | None, str] | None:
    """``(provider_id, model_id)`` for a stored voice assignment, else ``None``.

    Voice values use the same codec as Task assignments on ``__voice__``: a bare
    model id is the native Claude leg; a composite names a provider row.
    """
    return decode_subagent_binding(VOICE_PROVIDER_KEY, value)


def available_voice_subagents(
    exclude_profile_id: str | None = None,
) -> tuple[SubagentTarget, ...]:
    """The roster a VOICE call may dispatch: agents with a voice assignment.

    Voice mode has no caller provider to scope by — the call is funded by an
    OpenAI Realtime row that says nothing about where a worker should run — so
    eligibility is the agent's own ``__voice__`` entry, decoded into the leg it
    pins. A provider-bound assignment is dropped (never offered) when its row is
    missing or disabled, or the model fails the provider's validation, so a name
    the voice model reads back is always one dispatch accepts.
    """
    from app.db import database

    try:
        rows = database.get_all_profiles() or []
    except Exception:
        logger.debug("Could not list profiles for voice subagent discovery", exc_info=True)
        return ()

    from app.core.builtin_subagents import is_read_only_tool_surface

    provider_cache: dict[str, Any] = {}
    targets: list[SubagentTarget] = []
    for row in rows:
        target_id = str(row.get("id") or "").strip()
        if not target_id or target_id == exclude_profile_id:
            continue
        config = row.get("config") or {}
        binding = decode_voice_binding(
            normalized_provider_models(config).get(VOICE_PROVIDER_KEY)
        )
        if binding is None:
            continue
        provider_id, model = binding

        if provider_id:
            if provider_id not in provider_cache:
                provider_cache[provider_id] = _load_provider_row(provider_id)
            if not _validated_provider_model(
                provider_id, model, provider_cache[provider_id]
            ):
                continue

        targets.append(
            SubagentTarget(
                id=target_id,
                name=str(row.get("name") or target_id).strip() or target_id,
                description=_clean_description(row.get("description")),
                model=model,
                background_default=is_read_only_tool_surface(config.get("allowed_tools")),
                provider_id=provider_id,
                provider_pinned=True,
            )
        )

    return tuple(sorted(targets, key=lambda target: (target.name.casefold(), target.id)))


def resolve_subagent(
    ref: str, targets: Sequence[SubagentTarget]
) -> tuple[SubagentTarget | None, str | None]:
    """Resolve an ID or unambiguous name inside an already-admitted roster."""
    value = (ref or "").strip()
    if not value:
        return None, "Task requires a target subagent ID."
    for target in targets:
        if target.id == value:
            return target, None
    matches = [target for target in targets if target.name.casefold() == value.casefold()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        ids = ", ".join(sorted(target.id for target in matches))
        return None, f"Subagent name '{value}' is ambiguous; use an ID: {ids}."
    available = ", ".join(target.id for target in targets) or "none"
    return None, (
        f"Subagent '{value}' is not available on the current provider. "
        f"Available subagent IDs: {available}."
    )


@dataclass(frozen=True)
class TaskContract:
    description: str
    input_schema: dict[str, Any]
    targets: tuple[SubagentTarget, ...]


def build_task_contract(
    caller_profile_id: str | None,
    provider_id: str | None,
    additional_targets: Sequence[SubagentTarget] = (),
) -> TaskContract:
    """Build the provider-scoped model-facing Task contract."""
    from fluid.tools.fragments import SWARM_ROUTING

    targets = list(available_subagents(caller_profile_id, provider_id))

    # File/plugin agents borrow the caller's provider and selected model. They
    # are valid Task targets too, but a stored DB agent wins every ID/name
    # collision even when that DB agent is unavailable on this provider. Match
    # dispatch_task_tool's host-first resolution so schema and runtime cannot
    # disagree about a file target.
    reserved_ids = {target.id for target in targets}
    reserved_names = {target.name.casefold() for target in targets}
    try:
        from app.db import database

        for row in database.get_all_profiles() or []:
            target_id = str(row.get("id") or "").strip()
            target_name = str(row.get("name") or target_id).strip()
            if target_id:
                reserved_ids.add(target_id)
            if target_name:
                reserved_names.add(target_name.casefold())
    except Exception:
        logger.debug("Could not reserve DB agent names for Task contract", exc_info=True)

    for target in sorted(additional_targets, key=lambda item: (item.name.casefold(), item.id)):
        if not target.id or target.id in reserved_ids:
            continue
        if target.name.casefold() in reserved_names:
            continue
        targets.append(target)
        reserved_ids.add(target.id)
        reserved_names.add(target.name.casefold())

    resolved_targets = tuple(targets)
    if resolved_targets:
        # IDs and models only — each agent's description already rides the
        # system prompt's "# Available agent types" index (built from the same
        # roster), and repeating it here doubled the tokens for every session.
        roster = "\n".join(
            f"- {target.id} (default model: {target.model}) — {target.name}"
            for target in resolved_targets
        )
        availability = (
            "Available subagents on this provider (descriptions are under "
            "'# Available agent types' in the system prompt):\n"
            f"{roster}\nUse only these IDs; do not invent a model or provider."
        )
    else:
        availability = (
            "No subagents are configured and runnable on this provider. "
            "Do not call Task until the configuration changes."
        )

    # A background-default target is dispatched in the background whatever the
    # call says (task_dispatch._effective_task_mode). When EVERY reachable
    # target is one, ``mode`` is not a choice at all, so it is dropped from the
    # schema — models defeat the default simply because the key is there to
    # pass. On a mixed roster the key has to stay
    # for the write-capable targets; the description then names the agents that
    # ignore it, so an ignored value is documented rather than surprising.
    background_only_ids = [
        target.id for target in resolved_targets if target.background_default
    ]
    offer_mode = not resolved_targets or len(background_only_ids) < len(
        resolved_targets
    )

    if offer_mode:
        mode_text = (
            "mode='sync' waits for the report. mode='background' returns a "
            "worker session ID so the parent can continue independent work; "
            "completion is push-based, so do not busy-poll. Omitting mode "
            "takes the target's default: read-only research agents run in the "
            "background, every other agent runs sync. For a read-only research "
            "agent, omit mode; pass sync only when you cannot take a single "
            "further step without the report."
        )
        if background_only_ids:
            mode_text += (
                " These targets always run in the background and ignore mode "
                "entirely: " + ", ".join(background_only_ids) + "."
            )
    else:
        mode_text = (
            "Every target on this roster is a read-only research agent, so "
            "every dispatch runs in the background and there is no mode "
            "parameter. The worker's completion is announced to you; do not "
            "busy-poll."
        )

    # Documented once, here, because delegating sessions otherwise burn turns
    # rediscovering it by KeyError.
    sync_shape = (
        "A sync result is JSON with report_head (a bounded prefix), "
        "report_chars (the report's full length), and report_path (the whole "
        "report on disk) — there is NO 'report' key unless the file could not "
        "be written, in which case 'report' carries the full text and the note "
        "says so. Read report_path with Read(offset=..., limit=...), in ONE "
        "cell, only when report_head is insufficient; never print the whole "
        "file. A background result carries no report at all: it returns "
        "worker_session_id/task_id and the completion is announced later."
    )

    description = (
        "Delegate a self-contained task to one configured subagent. The "
        "subagent runs in an isolated session with its own prompt, memory, "
        "and tools; it does not see the parent conversation. Use delegation "
        "for independent or context-heavy work, not a quick lookup or single "
        f"tool call. {SWARM_ROUTING}\n\n{mode_text}\n\n{sync_shape}\n\n" + availability
    )

    agent_property: dict[str, Any] = {
        "type": "string",
        "description": "Target subagent ID from the provider-scoped roster above.",
    }
    if resolved_targets:
        agent_property["enum"] = [target.id for target in resolved_targets]

    properties: dict[str, Any] = {
        "agent": agent_property,
        "prompt": {
            "type": "string",
            "description": (
                "Self-contained assignment: objective, relevant paths/data, "
                "constraints, verification, and expected report. The subagent "
                "does not see the parent conversation."
            ),
        },
    }
    # The enum is a union for mixed-provider rosters. Dispatch revalidates the
    # selected ID against the chosen target's effective provider, not this union.
    from app.core.provider_agent import curated_model_ids

    model_ids: set[str] = set()
    provider_ids = {
        target.provider_id if target.provider_pinned else provider_id
        for target in resolved_targets
    }
    for target_provider in provider_ids:
        if target_provider:
            row = _load_provider_row(target_provider)
            if row and row.get("enabled"):
                model_ids.update(curated_model_ids(row))
    if model_ids:
        properties["model"] = {
            "type": "string",
            "enum": sorted(model_ids),
            "description": (
                "Optional model ID from the user's configured model list on the "
                "selected target's provider. Omit to use the roster's default "
                "model. This cannot switch providers; on mixed rosters an ID "
                "must belong to the chosen target's provider."
            ),
        }
    if offer_mode:
        mode_description = (
            "sync waits for the report; background returns the worker session "
            "ID immediately. For a read-only research agent, omit mode; pass "
            "sync only when you cannot take a single further step without the "
            "report. Omit to take the target's default: background for "
            "read-only research agents, sync for every other agent."
        )
        if background_only_ids:
            mode_description += (
                " Ignored for these always-background targets: "
                + ", ".join(background_only_ids)
                + "."
            )
        properties["mode"] = {
            "type": "string",
            "enum": ["sync", "background"],
            "description": mode_description,
        }
    properties["label"] = {
        "type": "string",
        "description": "Optional short progress label.",
    }

    return TaskContract(
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["agent", "prompt"],
            "additionalProperties": False,
        },
        targets=resolved_targets,
    )


def file_subagent_targets(
    registry: Mapping[str, Any] | None,
    selected_model: str | None,
) -> tuple[SubagentTarget, ...]:
    """Project discovered Fluid definitions into Task-contract targets."""
    if not registry:
        return ()
    from app.core.builtin_subagents import is_read_only_tool_surface

    model = str(selected_model or "current model").strip() or "current model"
    targets = []
    for key, definition in registry.items():
        if isinstance(definition, Mapping):
            name = str(definition.get("name") or key).strip()
            description = definition.get("description")
            tools = definition.get("tools")
        else:
            name = str(getattr(definition, "name", None) or key).strip()
            description = getattr(definition, "description", None)
            tools = getattr(definition, "tools", None)
        if not name:
            continue
        targets.append(
            SubagentTarget(
                id=name,
                name=name,
                description=_clean_description(description),
                model=model,
                # The definition's ``tools:`` frontmatter is what dispatch
                # classifies a file agent on (task_dispatch's
                # ``_definition_tool_allowlist``).
                background_default=is_read_only_tool_surface(
                    list(tools) if isinstance(tools, (list, tuple)) else None
                ),
            )
        )
    return tuple(targets)


def parent_subagent_guidance(
    *, task_available: bool = True, swarm_available: bool = True,
    monitor_available: bool = True, browser_available: bool = True,
) -> str:
    """Describe only workflows exposed by this session's effective surface."""
    from fluid.tools.fragments import END_TURN_ON_BACKGROUND, SWARM_ROUTING

    parts = []
    if task_available:
        parts.append(
            "# Subagent delegation\n"
            "Use only Task's provider-scoped roster IDs and listed models. "
            "An override never switches providers; omit it to keep the default. "
            "If the roster is empty, do not delegate. Give an isolated worker "
            "a self-contained brief, verified checkout/prerequisites, allowed "
            "scope and expected result. Choose the role that matches the work: "
            "rendered UI belongs to the frontend role when present, not a "
            "backend-only worker. Handle quick direct work yourself. "
            "Delegate only when the user authorizes subagents.\n"
            + (SWARM_ROUTING if swarm_available else
               "Use Task for independent or adaptive assignments; do not "
               "invent an unavailable workflow tool.")
        )
    if task_available or monitor_available:
        parts.append(
            "# Background work\n"
            "Background completion is push-based and wakes the owning "
            "session, including workers. Parking is not a final assignment "
            "report. Inspect completion before claiming the work is done. "
            + END_TURN_ON_BACKGROUND
            + " No sleep loops or status polling. "
            + ("Long builds, installs, suites and CI waits belong in Monitor."
               if monitor_available else
               "If no background runner is exposed, report that limitation; "
               "do not invent one.")
        )
    if browser_available:
        parts.append(
            "# Verifying a UI change\n"
            "Verify rendered UI in the browser at the affected viewport "
            "before the PR, not with unit tests alone. Discover the browser "
            "tools from the effective catalog when needed."
        )
    return "\n\n".join(parts)
