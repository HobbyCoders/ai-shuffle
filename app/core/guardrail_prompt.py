"""Provider safety checks, app side: the blocked-call prompt and its notes.

Fluid raises a ``ModelGuardrail`` permission request when a provider's safety
check declines a call (see ``fluid/fluid/engine/guardrail.py``). This module:

* fills in the model to offer when the provider named none
  (:func:`next_best_model`),
* asks the person through the ordinary approval queue in EVERY permission mode
  (:func:`ask`; a mode never approves a model switch on its own),
* builds the persisted ``guardrail_blocked`` / ``model_rerouted`` system notes
  (:func:`guardrail_blocked_note`, :func:`model_rerouted_note`).

The event contract the frontend builds on is in
``docs/internal/guardrail-contract.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fluid.engine.guardrail import (
    GUARDRAIL_TOOL,
    blocked_message,
    provider_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GUARDRAIL_NO_APPROVER_REASON",
    "GUARDRAIL_TOOL",
    "account_label",
    "ask",
    "guardrail_blocked_note",
    "model_rerouted_note",
    "next_best_model",
]

#: The stop reason recorded when nobody can be asked (background, REST and
#: other unattended turns): the turn ends with the blocked note at once.
GUARDRAIL_NO_APPROVER_REASON = "no one was there to approve continuing on another model"
#: The stop reason recorded when there is no model to offer.
NO_MODEL_REASON = "no other model to continue on"

# One step down within a Claude family, for a refusal Anthropic gave no
# category for. Keys are API ids; the app's aliases resolve through
# ``model_registry.API_MODEL_IDS`` first.
_CLAUDE_STEP_DOWN = {
    "claude-fable-5": "claude-opus-5",
    "claude-opus-5-5": "claude-opus-5",
    "claude-opus-5": "claude-opus-4-8",
    "claude-sonnet-5-5": "claude-sonnet-5",
}

# Categories another model does not help with: Anthropic's documented advice
# for reasoning extraction is to rephrase the request.
_NO_RETRY_CATEGORIES = frozenset({"reasoning_extraction"})


# What a person calls the company behind a provider row, by row type. A
# ``custom`` row (a self-hosted or other OpenAI-compatible server) goes by the
# name its owner gave it.
_ROW_TYPE_LABEL = {
    "openrouter": "OpenRouter", "openai": "OpenAI", "chatgpt": "OpenAI",
    "xai": "xAI", "grok": "xAI",
}


def _provider_row(provider_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not provider_id:
        return None
    try:
        from app.db.model_providers import get_model_provider

        row = get_model_provider(provider_id)
    except Exception:  # noqa: BLE001 — no row is the safe answer
        logger.debug("could not read provider %s for a safety-check note", provider_id, exc_info=True)
        return None
    return dict(row) if row else None


def _provider_models(provider_id: str) -> List[str]:
    """The model ids a provider row offers, in the row's own order."""
    row = _provider_row(provider_id)
    if not row:
        return []
    from app.core.model_override import _entries

    return [str(entry["id"]) for entry in _entries(row)]


def account_label(provider_id: Optional[str]) -> Optional[str]:
    """The provider's name for a provider-row session, else None.

    None leaves fluid's label (Anthropic for a Claude session). The client
    name alone would call every OpenAI-compatible server "OpenRouter".
    """
    row = _provider_row(provider_id)
    if not row:
        return None
    label = _ROW_TYPE_LABEL.get(str(row.get("provider_type") or "").lower())
    return label or (str(row.get("name") or "").strip() or None)


def next_best_model(
    requested: Optional[str], *, provider_id: Optional[str], category: Optional[str],
) -> Optional[str]:
    """The model to offer when the provider recommended none, else None.

    The rule (documented in the guardrail contract):

    1. ``reasoning_extraction`` gets no offer: another model does not help, the
       person rephrases instead.
    2. A provider-row session (OpenAI, Codex, OpenRouter, custom): the next
       model listed on the SAME provider row after the requested one, else the
       first other one listed there. Never another account.
    3. Claude (no provider row): one step down in the same family
       (``_CLAUDE_STEP_DOWN``); none past the end of the table.
    """
    if category in _NO_RETRY_CATEGORIES or not requested:
        return None
    if provider_id:
        models = _provider_models(provider_id)
        bare = requested.rsplit("/", 1)[-1] if requested not in models else requested
        others = [m for m in models if m != requested and m.rsplit("/", 1)[-1] != bare]
        if not others:
            return None
        try:
            index = next(
                i for i, m in enumerate(models)
                if m == requested or m.rsplit("/", 1)[-1] == bare
            )
        except StopIteration:
            return others[0]
        after = [m for m in models[index + 1:] if m in others]
        return after[0] if after else others[0]
    from app.core.model_registry import resolve_api_model_id

    api_id = resolve_api_model_id(requested) or requested
    return _CLAUDE_STEP_DOWN.get(api_id.split("[", 1)[0])


def _offer_message(data: Dict[str, Any], suggested: Optional[str]) -> str:
    message = blocked_message(
        data.get("provider"), data.get("category"), label=data.get("provider_label"),
    )
    if suggested:
        message += f" Continue this turn on {suggested}?"
    return message


async def ask(
    tool_input: Dict[str, Any], *, session_id: str, profile_id: Optional[str],
    provider_id: Optional[str], broadcast_func: Callable[[Dict[str, Any]], Awaitable[None]],
    tool_use_id: Optional[str], request_permission: Callable[..., Awaitable[Any]],
) -> Any:
    """Put a ``ModelGuardrail`` prompt to the person; the callback's answer.

    ``request_permission`` is query_engine's ``_request_permission`` (the
    approval queue with the awaiting-input badge, fanned out to every device
    and kept across reloads). Allow returns the prompt's input with
    ``suggested_model`` filled, which the engine retries on; anything else
    (deny, defer, away, timed out) is a deny whose message becomes the
    ``guardrail_blocked`` note's ``reason``.
    """
    from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
    from app.core.permission_handler import _DeferSentinel

    data = dict(tool_input or {})
    suggested = data.get("suggested_model")
    if not (isinstance(suggested, str) and suggested.strip()):
        suggested = next_best_model(
            data.get("requested_model"), provider_id=provider_id,
            category=data.get("category"),
        )
        data["suggested_model"] = suggested
        data["suggested_by"] = "app" if suggested else None
    else:
        data["suggested_by"] = "provider"
    if not suggested:
        return PermissionResultDeny(message=NO_MODEL_REASON)
    label = account_label(provider_id) or provider_label(data.get("provider"))
    data["provider_label"] = label
    data["message"] = _offer_message(data, suggested)
    try:
        result = await request_permission(
            request_id=f"guardrail-{tool_use_id or session_id}",
            session_id=session_id,
            profile_id=profile_id or "",
            tool_name=GUARDRAIL_TOOL,
            tool_input=data,
            broadcast_func=broadcast_func,
            tool_use_id=tool_use_id,
            title=data["message"],
            display_name="Safety check",
            description=data["message"],
            decision_reason=data.get("explanation") or (
                f"{label} declined the request on {data.get('requested_model')}. "
                f"Continuing retries it on {suggested} for this turn only."
            ),
            skip_saved_rules=True,
            safety_check=True,
        )
    except Exception:  # noqa: BLE001 — a failed prompt never switches models
        logger.exception("Safety-check prompt failed for %s", session_id)
        return PermissionResultDeny(message="the prompt could not be shown")
    if isinstance(result, _DeferSentinel):
        return PermissionResultDeny(message="the owner was not available to answer")
    if isinstance(result, PermissionResultAllow) or getattr(result, "behavior", None) == "allow":
        answered = getattr(result, "updated_input", None)
        chosen = answered.get("model") if isinstance(answered, dict) else None
        return PermissionResultAllow(updated_input={
            **data, "model": chosen if isinstance(chosen, str) and chosen else suggested,
        })
    return result


def _blocked_content(
    provider: Optional[str], category: Optional[str], outcome: str,
    retry: Optional[str], label: Optional[str],
) -> str:
    content = blocked_message(provider, category, label=label)
    if outcome == "continued" and retry:
        return content + f" Continued this turn on {retry}."
    return content + " The turn stopped."


def guardrail_blocked_note(event: Any) -> Dict[str, Any]:
    """The ``guardrail_blocked`` system note's data for a GuardrailBlocked event."""
    provider = getattr(event, "provider", None)
    category = getattr(event, "category", None)
    outcome = getattr(event, "outcome", None) or "stopped"
    retry = getattr(event, "retry_model", None)
    reason = getattr(event, "reason", None)
    return {
        "content": _blocked_content(provider, category, outcome, retry, None),
        "outcome": outcome,
        "provider": provider,
        "provider_label": provider_label(provider),
        "category": category,
        "explanation": getattr(event, "explanation", None),
        "requested_model": getattr(event, "requested_model", None),
        "suggested_model": getattr(event, "suggested_model", None),
        "retry_model": retry,
        "reason": reason,
        "request_id": getattr(event, "request_id", None),
        "turn_id": getattr(event, "turn_id", None),
    }


def relabel_blocked_note(data: Dict[str, Any], provider_id: Optional[str]) -> Dict[str, Any]:
    """A ``guardrail_blocked`` note named for the session's provider row."""
    label = account_label(provider_id)
    if not label or not isinstance(data, dict):
        return data
    return {
        **data,
        "provider_label": label,
        "content": _blocked_content(
            data.get("provider"), data.get("category"), data.get("outcome") or "stopped",
            data.get("retry_model"), label,
        ),
    }


def model_rerouted_note(
    data: Dict[str, Any], provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``model_rerouted`` system note's data from a ``model_served`` message."""
    label = account_label(provider_id) or provider_label(data.get("provider"))
    served = data.get("served_model")
    requested = data.get("requested_model")
    content = f"{label} answered with {served} instead of {requested}."
    category = data.get("category")
    if category:
        content += f" Safety check: {str(category).replace('_', ' ')}."
    return {
        "content": content,
        "provider": data.get("provider"),
        "provider_label": label,
        "requested_model": requested,
        "served_model": served,
        "reason": data.get("reason"),
        "category": category,
    }
