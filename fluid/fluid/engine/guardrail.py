"""Provider safety checks: served-model comparison and the blocked-call prompt.

Shared by the native loop (Anthropic ``stop_reason: "refusal"``, an OpenAI or
chat-completions content filter before any output) and the delegated Claude CLI
leg (``model_refusal_no_fallback``). A blocked call is never retried on another
model on the engine's own say-so: the engine raises an ordinary
:class:`~fluid.events.PermissionRequest` under the pseudo-tool
:data:`GUARDRAIL_TOOL` and waits for the answer like any other prompt.

* **allow** retries the blocked call on the offered model and keeps the rest of
  the turn on it. The responder may pick the model: ``updated_input["model"]``
  wins over ``input["suggested_model"]``. The next turn runs on the session's
  own model again.
* **deny** (or no answer within ``permission_timeout``, or an interrupt) ends
  the turn with ``stop_reason="refusal"``.

Either way a :class:`~fluid.events.GuardrailBlocked` event records the outcome.
"""

from __future__ import annotations

import re
from typing import Any

#: The pseudo-tool a blocked call's prompt is raised under. Not a registered
#: tool: no saved rule or mode may approve it, only a person.
GUARDRAIL_TOOL = "ModelGuardrail"

#: Anthropic's per-category fallback recommendations, keyed by the requested
#: model. Anthropic does not publish its routing; this is the table Claude Code
#: 2.1.316 ships for the same retry (``refusals-and-fallback`` docs: retry the
#: same request on another Claude model). ``reasoning_extraction`` has no
#: fallback by design (rephrase instead). An unlisted model uses ``_DEFAULT``.
_ANTHROPIC_DEFAULT = {
    "bio": "claude-opus-5", "cyber": "claude-opus-4-8", "frontier_llm": "claude-opus-5",
}
_ANTHROPIC_FALLBACKS: dict[str, dict[str, str]] = {
    "claude-opus-5-5": dict(_ANTHROPIC_DEFAULT),
    "claude-opus-5": {"cyber": "claude-opus-4-8"},
    "claude-sonnet-5-5": {"cyber": "claude-sonnet-5", "frontier_llm": "claude-sonnet-5"},
}

_PROVIDER_LABEL = {
    "anthropic": "Anthropic", "claude": "Anthropic", "openai": "OpenAI",
    "openai_responses": "OpenAI", "codex": "OpenAI", "openrouter": "OpenRouter",
}

_FAMILIES = ("opus", "sonnet", "haiku")

_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def _base(model: str) -> str:
    m = (model or "").strip().lower()
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    m = m.split("[", 1)[0]  # "claude-opus-5-5[1m]"
    return _DATE_SUFFIX.sub("", m)


def same_model(requested: str | None, served: str | None) -> bool:
    """Do these two ids name the same model?

    Case, a provider prefix (``openai/``), a context-size suffix (``[1m]``) and
    a dated snapshot suffix (``-20250929``, ``-2026-08-01``) are ignored, so a
    pinned snapshot of the requested alias is not a reroute. Unknown on either
    side counts as the same: no evidence of a reroute.
    """
    if not requested or not served:
        return True
    want, got = _base(requested), _base(served)
    if not any(ch.isdigit() for ch in want):
        # An alias ("opus", "sonnet", "default"): the CLI resolves it to a
        # dated id. A family alias matches any model of that family; anything
        # else gives no evidence either way.
        return want not in _FAMILIES or want in got
    return want == got


def anthropic_fallback(model: str | None, category: str | None) -> str | None:
    """The Claude model Anthropic recommends retrying ``category`` on, else None."""
    if not category:
        return None
    routes = _ANTHROPIC_FALLBACKS.get(_base(model or ""), _ANTHROPIC_DEFAULT)
    target = routes.get(category)
    if target and same_model(target, model):
        return None
    return target


def provider_label(provider: str | None) -> str:
    """The name a person knows the provider by ("Anthropic", "OpenAI")."""
    key = (provider or "").strip().lower()
    return _PROVIDER_LABEL.get(key, provider or "The provider")


def blocked_message(provider: str | None, category: str | None) -> str:
    """One plain sentence: "Blocked by Anthropic's safety check (cyber)."."""
    label = provider_label(provider)
    owner = f"{label}'s" if label != "The provider" else "the provider's"
    suffix = f" ({category.replace('_', ' ')})" if category else ""
    return f"Blocked by {owner} safety check{suffix}."


def prompt_input(
    *, provider: str, requested_model: str, category: str | None,
    explanation: str | None, suggested_model: str | None,
    request_id: str | None = None, partial_output: bool = False,
) -> dict[str, Any]:
    """The ``input`` of the :data:`GUARDRAIL_TOOL` permission prompt."""
    message = blocked_message(provider, category)
    if suggested_model:
        message += f" Continue this turn on {suggested_model}?"
    return {
        "guardrail": True,
        "provider": provider,
        "provider_label": provider_label(provider),
        "category": category,
        "explanation": explanation,
        "requested_model": requested_model,
        "suggested_model": suggested_model,
        "request_id": request_id,
        "partial_output_discarded": partial_output,
        "message": message,
    }


__all__ = [
    "GUARDRAIL_TOOL",
    "anthropic_fallback",
    "blocked_message",
    "prompt_input",
    "provider_label",
    "same_model",
]
