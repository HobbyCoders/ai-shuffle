"""Per-model effort and thinking rules for the Anthropic Messages API.

Claude models differ in what they accept, and the API rejects the whole
request rather than clamping:

* ``output_config.effort``: Opus 4.7/4.8, Sonnet 5, Opus 5 and later, and
  Fable/Mythos take low, medium, high, xhigh and max. Opus 4.6 and Sonnet 4.6
  take everything but xhigh. Opus 4.5 takes low, medium and high. Haiku 4.5,
  Sonnet 4.5 and older models reject the parameter.
* ``thinking``: Opus 4.7 and later reject ``{"type": "enabled",
  "budget_tokens": N}`` and take ``{"type": "adaptive"}``. Fable, Mythos and
  Opus 5.5 and later always think and reject ``{"type": "disabled"}``; Opus 5
  rejects it at xhigh and max effort. Models before 4.6 have no adaptive mode.

Source: the Anthropic API reference as bundled with the claude-api skill
(cached 2026-06-24), not a live probe.

A model id this module does not recognise as Claude passes through unchanged:
the host chose the value for a model fluid knows nothing about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ClaudeModel:
    tier: str  # opus | sonnet | haiku | fable | mythos
    major: int
    minor: int


# ``claude-opus-4-6``, ``claude-sonnet-5``, ``claude-opus-5-5[1m]``,
# ``anthropic.claude-opus-4-8-v1:0``. The minor version is one or two digits,
# so a date snapshot (``claude-sonnet-4-20250514``) reads as minor 0.
_MODERN = re.compile(
    r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)"
)
# Claude 3.x ids put the version first: ``claude-3-5-sonnet-20241022``.
_LEGACY = re.compile(r"claude-(\d+)(?:-(\d{1,2}))?-(opus|sonnet|haiku)")


def parse_claude_model(model: str | None) -> ClaudeModel | None:
    lowered = (model or "").lower()
    match = _MODERN.search(lowered)
    if match:
        return ClaudeModel(match.group(1), int(match.group(2)), int(match.group(3) or 0))
    match = _LEGACY.search(lowered)
    if match:
        return ClaudeModel(match.group(3), int(match.group(1)), int(match.group(2) or 0))
    return None


def _version(model: ClaudeModel) -> tuple[int, int]:
    return (model.major, model.minor)


def _always_thinks(model: ClaudeModel) -> bool:
    """Omitting ``thinking`` runs adaptive thinking on these models."""
    if model.tier in ("fable", "mythos"):
        return True
    if model.tier in ("opus", "sonnet"):
        return model.major >= 5
    return False


def _rejects_disabled(model: ClaudeModel, effort: str | None) -> bool:
    if model.tier in ("fable", "mythos"):
        return True
    if model.tier == "opus" and _version(model) >= (5, 5):
        return True
    if model.tier == "opus" and model.major == 5:
        return effort in ("xhigh", "max")
    return False


def _accepted_efforts(model: ClaudeModel) -> tuple[str, ...]:
    if model.tier in ("fable", "mythos"):
        return EFFORT_LADDER
    if model.tier == "haiku":
        return ()
    version = _version(model)
    if model.tier == "opus":
        if version >= (4, 7):
            return EFFORT_LADDER
        if version >= (4, 6):
            return ("low", "medium", "high", "max")
        if version >= (4, 5):
            return ("low", "medium", "high")
        return ()
    if model.tier == "sonnet":
        if version >= (5, 0):
            return EFFORT_LADDER
        if version >= (4, 6):
            return ("low", "medium", "high", "max")
        return ()
    return ()


def clamp_effort(model: str | None, effort: str | None) -> str | None:
    """The effort to send for ``model``.

    A level the model does not accept becomes the highest accepted level at
    or below it (xhigh on Opus 4.6 becomes high). A model that rejects the
    parameter gets none. Values outside the ladder and non-Claude models pass
    through unchanged.
    """
    if effort is None or effort not in EFFORT_LADDER:
        return effort
    parsed = parse_claude_model(model)
    if parsed is None:
        return effort
    accepted = _accepted_efforts(parsed)
    if effort in accepted:
        return effort
    if not accepted:
        return None
    rank = EFFORT_LADDER.index(effort)
    lower = [level for level in accepted if EFFORT_LADDER.index(level) <= rank]
    return lower[-1] if lower else accepted[0]


def resolve_thinking(
    model: str | None,
    thinking: dict[str, Any] | None,
    effort: str | None = None,
) -> dict[str, Any] | None:
    """The ``thinking`` field to send for ``model``, or None to omit it.

    ``thinking`` is the host's explicit setting in API shape. A dict with a
    ``display`` and no ``type`` means "the model's default mode, shown this
    way": it becomes adaptive with that display on the models that think by
    default and is dropped elsewhere, so a display setting never turns
    thinking on. ``effort`` is the value actually sent (after
    :func:`clamp_effort`).
    """
    if not thinking:
        return None
    parsed = parse_claude_model(model)
    if parsed is None:
        return dict(thinking)
    kind = thinking.get("type")
    display = thinking.get("display")
    shown = {"display": display} if display and _version(parsed) >= (4, 6) else {}
    if kind is None:
        return {"type": "adaptive", **shown} if shown and _always_thinks(parsed) else None
    if kind == "disabled":
        if _rejects_disabled(parsed, effort):
            return None
        return {"type": "disabled"}
    if kind == "enabled" and _version(parsed) >= (4, 7):
        # Budgets are removed from 4.7 on; adaptive is the thinking mode.
        return {"type": "adaptive", **shown}
    if kind == "adaptive" and _version(parsed) < (4, 6):
        # No adaptive mode before 4.6, and no budget to translate it into.
        return None
    return {**{k: v for k, v in thinking.items() if k != "display"}, **shown}
