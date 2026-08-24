"""Provider-scoped model assignments and model-facing subagent discovery.

``config.provider_models`` is the ONE model binding an agent still carries: the
model it runs on when another agent dispatches it as a Task subagent. Top-level
chat sessions stopped reading an agent's model in v79 — they carry their own,
picked at creation — so the legacy ``config.provider`` / ``config.model`` pair
survives here only as a fallback for agents configured before that move.

The current caller provider chooses the map entry; a model-authored Task call
never chooses or changes providers.
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


def provider_key(provider_id: str | None) -> str:
    """Return the config-map key for a provider binding."""
    return str(provider_id).strip() if provider_id else NATIVE_PROVIDER_KEY


def normalized_provider_models(config: Mapping[str, Any]) -> dict[str, str]:
    """Return valid stored assignments plus the legacy top-level binding.

    The legacy fallback is intentionally provider-local. An old native-Claude
    agent becomes available on native Claude, and an old provider-bound agent
    becomes available on that provider. Neither is inferred onto other
    providers. Agents saved since v79 have no top-level model at all, so for
    them the stored map is the whole answer.
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
    and agents with a valid model assignment on the current provider. The same
    function feeds Task's schema and its runtime admission check.
    """
    if not caller_profile_id:
        return ()

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

    # Fail closed for a non-native provider that is missing, disabled, or has
    # no valid curated target model. Do not reuse ProviderAgent's retired
    # cross-provider gateway gate here: Task stays on the caller's provider,
    # and Fluid already serves providers such as OpenRouter natively.
    provider = None
    if provider_id:
        try:
            provider = database.get_model_provider(provider_id, include_token=False)
        except TypeError:
            # Test doubles and older database adapters may not accept the
            # include_token keyword.
            try:
                provider = database.get_model_provider(provider_id)
            except Exception:
                logger.debug("Could not load subagent provider", exc_info=True)
                return ()
        except Exception:
            logger.debug("Could not load subagent provider", exc_info=True)
            return ()
        if not provider or not provider.get("enabled"):
            return ()
    targets = []
    try:
        rows = database.get_all_profiles() or []
    except Exception:
        logger.debug("Could not list profiles for subagent discovery", exc_info=True)
        return ()
    for row in rows:
        target_id = str(row.get("id") or "").strip()
        if not target_id or target_id == caller_profile_id:
            continue
        if allowlist is not None and target_id not in allowlist:
            continue

        config = row.get("config") or {}
        model = model_for_provider(config, provider_id)
        if not model:
            continue
        if provider_id:
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
                continue
            if not validation.ok:
                continue

        targets.append(
            SubagentTarget(
                id=target_id,
                name=str(row.get("name") or target_id).strip() or target_id,
                description=_clean_description(row.get("description")),
                model=model,
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
            f"- {target.id} (model: {target.model}) — {target.name}"
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

    description = (
        "Delegate a self-contained task to one configured subagent. The "
        "subagent runs in an isolated session with its own prompt, memory, "
        "and tools; it does not see the parent conversation. Use delegation "
        "for independent or context-heavy work, not a quick lookup or single "
        "tool call. mode='sync' waits for the report. mode='background' "
        "returns a worker session ID so the parent can continue independent "
        "work; completion is push-based, so do not busy-poll.\n\n"
        + availability
    )

    agent_property: dict[str, Any] = {
        "type": "string",
        "description": "Target subagent ID from the provider-scoped roster above.",
    }
    if resolved_targets:
        agent_property["enum"] = [target.id for target in resolved_targets]

    return TaskContract(
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "agent": agent_property,
                "prompt": {
                    "type": "string",
                    "description": (
                        "Self-contained assignment: objective, relevant paths/data, "
                        "constraints, verification, and expected report. The subagent "
                        "does not see the parent conversation."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["sync", "background"],
                    "description": (
                        "sync (default) waits for the report; background returns "
                        "the worker session ID immediately."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Optional short progress label.",
                },
            },
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
    model = str(selected_model or "current model").strip() or "current model"
    targets = []
    for key, definition in registry.items():
        if isinstance(definition, Mapping):
            name = str(definition.get("name") or key).strip()
            description = definition.get("description")
        else:
            name = str(getattr(definition, "name", None) or key).strip()
            description = getattr(definition, "description", None)
        if not name:
            continue
        targets.append(
            SubagentTarget(
                id=name,
                name=name,
                description=_clean_description(description),
                model=model,
            )
        )
    return tuple(targets)


def parent_subagent_guidance(
    caller_profile_id: str | None, provider_id: str | None
) -> str:
    """Compact system-prompt guidance aligned with the dynamic Task schema."""
    # The park doctrine is fluid's canonical fragment (spliced verbatim into
    # Task's and Monitor's own descriptions) so the copies cannot drift.
    from fluid.tools.fragments import END_TURN_ON_BACKGROUND

    return (
        "# Subagent delegation\n"
        "Configured agents are exposed to you as subagents through Task. "
        "Task's schema lists the only DB and file/plugin subagents available "
        "on the current provider, including each assigned model. Use only those "
        "IDs; if the list is empty, do not call Task.\n"
        "A subagent receives an isolated session, not this conversation, so give "
        "it a self-contained brief with relevant context and expected output. "
        "Delegate independent or context-heavy work; handle quick direct work "
        "yourself. Background completion is push-based: continue useful work "
        f"and do not poll in a loop. {END_TURN_ON_BACKGROUND} "
        "Never invent a subagent ID, model, or provider."
    )
