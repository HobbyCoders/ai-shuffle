"""
Claude model registry — single source of truth for the backend.

Add/remove a model here and every Pydantic default, MCP tool enum, and
fast-mode eligibility check across the backend picks it up automatically.
Callers should never hardcode a model ID inline — import from this module.

Mirrors ``frontend/src/lib/constants/models.ts``. The two registries must
stay in sync; the frontend can hydrate from ``GET /api/models`` to verify
at runtime. Any addition/removal should land in both files in the same
commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

ModelFamily = Literal["haiku", "sonnet", "opus"]


@dataclass(frozen=True)
class ModelOption:
    """A selectable Claude model and its capabilities."""

    value: str
    """Canonical model ID persisted to storage and sent to the SDK."""

    label: str
    """Human-facing label for dropdowns and badges."""

    family: ModelFamily
    """Model family — drives badge colour and fast-mode eligibility."""

    context_window: int
    """Maximum input context in tokens (200k standard, 1M for [1m] variants)."""

    fast_mode_eligible: bool
    """Whether this model supports the 'fast mode' per-session toggle."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order here is the order shown in every dropdown in the UI and the order
# reported by ``GET /api/models``.
MODELS: Tuple[ModelOption, ...] = (
    ModelOption("claude-haiku-4-5",      "Haiku 4.5",       "haiku",  200_000,   False),
    ModelOption("claude-sonnet-4-6",     "Sonnet 4.6",      "sonnet", 200_000,   False),
    ModelOption("claude-sonnet-4-6[1m]", "Sonnet 4.6 1M",   "sonnet", 1_000_000, False),
    ModelOption("claude-opus-4-6",       "Opus 4.6",        "opus",   200_000,   True),
    ModelOption("claude-opus-4-6[1m]",   "Opus 4.6 1M",     "opus",   1_000_000, True),
    ModelOption("claude-opus-4-7",       "Opus 4.7",        "opus",   200_000,   True),
    ModelOption("claude-opus-4-7[1m]",   "Opus 4.7 1M",     "opus",   1_000_000, True),
)

DEFAULT_MODEL = "claude-sonnet-4-6"
"""Default model used when nothing is configured yet."""

INHERIT_VALUE = ""
"""Sentinel meaning 'inherit from parent' (used by subagents)."""

# Legacy short slugs still persisted in older user data. Map to the closest
# canonical model so older profiles/subagents round-trip cleanly. We treat
# these as read-only aliases — storage is rewritten on the next save, never
# eagerly.
LEGACY_MODEL_MAP: Dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "sonnet[1m]": "claude-sonnet-4-6[1m]",
    "opus": "claude-opus-4-7",
    "opus[1m]": "claude-opus-4-7[1m]",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BY_VALUE: Dict[str, ModelOption] = {m.value: m for m in MODELS}


def normalize_model(value: Optional[str]) -> str:
    """Resolve a stored model string to its canonical form.

    - ``None`` / empty → ``""`` (inherit / unset)
    - Legacy short slug → canonical full ID
    - Any other string → returned as-is (custom models pass through).
    """
    if not value:
        return INHERIT_VALUE
    return LEGACY_MODEL_MAP.get(value, value)


def is_preset_model(value: Optional[str]) -> bool:
    """True when ``value`` (after normalization) is one of the selectable presets."""
    return normalize_model(value) in _BY_VALUE


def get_model(value: Optional[str]) -> Optional[ModelOption]:
    """Look up a ``ModelOption`` by stored value, honouring legacy slugs."""
    normalized = normalize_model(value)
    return _BY_VALUE.get(normalized)


def get_model_label(value: Optional[str]) -> str:
    """Human-readable label; falls back to the raw value, then 'Default'."""
    match = get_model(value)
    if match is not None:
        return match.label
    return value if value else "Default"


def get_model_family(value: Optional[str]) -> Optional[ModelFamily]:
    """Resolved family, or ``None`` for unknown/custom values."""
    match = get_model(value)
    return match.family if match is not None else None


def get_context_window(value: Optional[str]) -> int:
    """Context window for the given model; defaults to 200k for unknowns."""
    match = get_model(value)
    return match.context_window if match is not None else 200_000


def is_fast_mode_eligible(value: Optional[str]) -> bool:
    """Whether this model supports the per-session 'fast mode' toggle.

    Use this instead of string-matching ``"opus" in model`` — the heuristic
    misfires on any future model whose name happens to contain "opus" but
    does not actually support fast mode, and vice versa.
    """
    match = get_model(value)
    return match.fast_mode_eligible if match is not None else False


def model_values() -> List[str]:
    """All canonical model IDs, for use in Pydantic ``Literal`` / JSON-schema enums."""
    return [m.value for m in MODELS]


def registry_payload() -> Dict[str, object]:
    """Serialized registry for the ``GET /api/models`` endpoint."""
    return {
        "models": [
            {
                "value": m.value,
                "label": m.label,
                "family": m.family,
                "context_window": m.context_window,
                "fast_mode_eligible": m.fast_mode_eligible,
            }
            for m in MODELS
        ],
        "default": DEFAULT_MODEL,
        "inherit_value": INHERIT_VALUE,
        "legacy_map": dict(LEGACY_MODEL_MAP),
    }
