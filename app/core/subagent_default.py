"""The owner's one "subagent default" model and the models agents may choose.

Every agent the lead dispatches runs on, in order (#1827):

1. a model the lead names for this task, only when it is the default, the
   agent's own pin, or in ``allowed``;
2. the agent's pinned model for the caller's provider (``provider_models``);
3. this global default;
4. nothing: the start is refused and the owner is asked once to choose one.
   An unpinned agent never falls back to the session's model.

Stored as ONE shared system setting (``subagent_default``, JSON
``{"model": ..., "allowed": [...]}``), like the text-generation model: the
network's ``system_settings`` collection syncs it to every worker of the
account, and the dispatch path reads it without an account-hub lookup.

Values use the app's model id format: a Claude registry alias (``sonnet``) or
a ``prov:<providerId>:<modelId>`` composite. Writes are validated against the
models this install can run (the Claude registry, or an enabled provider's
curated list); reads tolerate anything, so a value that stopped being
available is reported, never crashes a dispatch.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = "subagent_default"

# The page offers a short list drawn from the owner's favourites; a cap keeps
# the Task schema (which lists these ids) small.
MAX_ALLOWED = 24

NOTIFICATION_CATEGORY = "subagent_default_needed"

# Where the owner chooses it, named in every refusal so neither the lead agent
# nor the owner has to guess.
SETTINGS_HINT = "The owner sets it on the Agents page (Subagent default)."


@dataclass(frozen=True)
class SubagentDefault:
    model: Optional[str] = None
    allowed: Tuple[str, ...] = ()


def _clean_list(values: Any) -> Tuple[str, ...]:
    seen: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        if isinstance(value, str) and value.strip() and value.strip() not in seen:
            seen.append(value.strip())
    return tuple(seen)


def read() -> SubagentDefault:
    """The stored setting, shape-checked only. Never raises."""
    try:
        from app.db import database

        raw = database.get_system_setting(SETTING_KEY)
    except Exception:  # noqa: BLE001 — an unreadable setting reads as unset
        logger.debug("Could not read the subagent default", exc_info=True)
        return SubagentDefault()
    if not raw:
        return SubagentDefault()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return SubagentDefault()
    if not isinstance(data, dict):
        return SubagentDefault()
    model = data.get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else None
    return SubagentDefault(model=model, allowed=_clean_list(data.get("allowed")))


def normalize_model(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(app-format id, None)`` for a model this install can run, else ``(None, reason)``.

    A Claude id normalizes onto its registry alias (``claude-opus-4-6`` ->
    ``opus``). A composite must name an enabled provider whose curated list
    holds the model, the same check an agent's pin passes at roster time.
    """
    if not isinstance(value, str) or not value.strip():
        return None, "A model id is required."
    text = value.strip()
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(text)
    if decoded is not None:
        from app.core.provider_agent import validate_provider_model

        validation = validate_provider_model(decoded[0], decoded[1], require_servable=False)
        if not validation.ok:
            return None, validation.reason or f"Model '{text}' is not available."
        return f"prov:{decoded[0]}:{decoded[1]}", None
    if text.startswith("prov:"):
        return None, f"'{text}' is not a valid prov:<providerId>:<modelId> id."
    if is_native_claude_model(text):
        from app.core.model_registry import model_values, normalize_model as _alias

        alias = _alias(text)
        if alias in model_values():
            return alias, None
        return None, (
            f"'{text}' is not a Claude model this app offers. "
            f"Choose one of: {', '.join(model_values())}."
        )
    return None, (
        f"'{text}' is not a Claude model. A provider's model is written "
        "prov:<providerId>:<modelId>."
    )


def write(model: Optional[str], allowed: Iterable[Any]) -> SubagentDefault:
    """Validate and save the whole setting. Raises ``ValueError`` on a bad id."""
    from app.db import database

    clean_model: Optional[str] = None
    if model is not None and (not isinstance(model, str) or model.strip()):
        clean_model, error = normalize_model(model)
        if error:
            raise ValueError(f"model: {error}")
    items = list(allowed or [])
    if len(items) > MAX_ALLOWED:
        raise ValueError(f"allowed: at most {MAX_ALLOWED} models.")
    clean_allowed: list[str] = []
    for item in items:
        normalized, error = normalize_model(item)
        if error or not normalized:
            raise ValueError(f"allowed: {error}")
        if normalized not in clean_allowed:
            clean_allowed.append(normalized)
    database.set_system_setting(
        SETTING_KEY, json.dumps({"model": clean_model, "allowed": clean_allowed})
    )
    if clean_model:
        # A later "no default" state (cleared again) may ask once more.
        reset_asked()
    return SubagentDefault(model=clean_model, allowed=tuple(clean_allowed))


def usable(value: Optional[str]) -> Optional[str]:
    """``value`` normalized when it can still run here, else ``None``."""
    if not value:
        return None
    normalized, error = normalize_model(value)
    return None if error else normalized


def effective() -> SubagentDefault:
    """The stored setting with entries that can no longer run dropped."""
    stored = read()
    allowed: list[str] = []
    for item in stored.allowed:
        normalized = usable(item)
        if normalized and normalized not in allowed:
            allowed.append(normalized)
    return SubagentDefault(model=usable(stored.model), allowed=tuple(allowed))


def payload() -> Dict[str, Any]:
    """The GET body: the stored choice."""
    stored = read()
    return {"model": stored.model, "allowed": list(stored.allowed)}


# ---------------------------------------------------------------------------
# Asking the owner once
# ---------------------------------------------------------------------------

_asked = False
_PENDING: set = set()


def reset_asked() -> None:
    global _asked
    _asked = False


def ask_owner_once(session_id: Optional[str] = None) -> bool:
    """Notify the owner, once per process, that subagents need a default.

    The Task refusal tells the lead agent to pass the request on; this bell
    item reaches the owner on every device even if the lead does not. Returns
    True when a notification was scheduled. Never raises.
    """
    global _asked
    if _asked:
        return False
    _asked = True
    try:
        import asyncio

        from app.services.notification_service import emit_notification

        coro = emit_notification(
            "admin",
            severity="warning",
            title="Choose a model for subagents",
            message=(
                "An agent tried to start a subagent, but no subagent default "
                "model is set and that agent has no model of its own. Choose "
                "the subagent default on the Agents page; every agent without "
                "its own model will use it."
            ),
            category=NOTIFICATION_CATEGORY,
            session_id=session_id,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Hold a reference until it finishes: the loop keeps only a weak one.
            task = loop.create_task(coro)
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
        else:
            asyncio.run(coro)
        return True
    except Exception:  # noqa: BLE001 — the refusal still tells the lead agent
        logger.warning("Could not notify the owner about the subagent default", exc_info=True)
        return False


def missing_default_error(agent_label: str, allowed: Tuple[str, ...] = ()) -> str:
    """The refusal a lead agent gets for an unpinned agent with no default."""
    text = (
        f"Subagent '{agent_label}' has no model: it is not pinned to one and the "
        "owner has not chosen a subagent default yet, and a subagent never runs "
        "on this session's model by fallback. Nothing was dispatched. Tell the "
        "owner to choose a subagent default (they have been notified). "
        + SETTINGS_HINT
        + " Do not retry this agent until they have."
    )
    if allowed:
        text += (
            " Until then you may pass model= one of the models the owner allows "
            f"for subagents: {', '.join(allowed)}."
        )
    return text
