"""
Claude model registry — single source of truth for the backend.

Add/remove a model here and every Pydantic default, MCP tool enum, and
model-family check across the backend picks it up automatically.
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
    """Model family — drives badge colour."""

    context_window: int
    """Maximum input context in tokens (always the standard 200k window;
    the 1M window is opt-in via the ``[1m]`` suffix, see ``CONTEXT_1M_SUFFIX``)."""

    supports_1m: bool = False
    """Whether this model can run with the 1M context window (``[1m]`` suffix)."""


# ---------------------------------------------------------------------------
# 1M context modifier
# ---------------------------------------------------------------------------

# The 1M context window is not a separate model — it is a capability toggled
# on a base model by appending this suffix to the stored model ID. The UI
# exposes it as a checkbox rather than as duplicate dropdown entries.
CONTEXT_1M_SUFFIX = "[1m]"
CONTEXT_1M_WINDOW = 1_000_000


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order here is the order shown in every dropdown in the UI and the order
# reported by ``GET /api/models``. Only base models live here; the 1M variants
# are derived on demand via ``CONTEXT_1M_SUFFIX``.
MODELS: Tuple[ModelOption, ...] = (
    ModelOption("claude-haiku-4-5",  "Haiku 4.5",  "haiku",  200_000, supports_1m=False),
    ModelOption("claude-sonnet-4-6", "Sonnet 4.6", "sonnet", 200_000, supports_1m=True),
    ModelOption("claude-opus-4-6",   "Opus 4.6",   "opus",   200_000, supports_1m=True),
    ModelOption("claude-opus-4-7",   "Opus 4.7",   "opus",   200_000, supports_1m=True),
    ModelOption("claude-opus-4-8",   "Opus 4.8",   "opus",   200_000, supports_1m=True),
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
    "opus": "claude-opus-4-8",
    "opus[1m]": "claude-opus-4-8[1m]",
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


def has_1m_context(value: Optional[str]) -> bool:
    """True when the (normalized) model opts into the 1M context window."""
    normalized = normalize_model(value)
    return bool(normalized) and normalized.endswith(CONTEXT_1M_SUFFIX)


def strip_context_suffix(value: Optional[str]) -> str:
    """The base model ID with any ``[1m]`` suffix removed (normalized first)."""
    normalized = normalize_model(value)
    if normalized.endswith(CONTEXT_1M_SUFFIX):
        return normalized[: -len(CONTEXT_1M_SUFFIX)]
    return normalized


def with_context_suffix(value: Optional[str], enabled: bool) -> str:
    """Apply or remove the ``[1m]`` suffix on a model ID.

    Inherit / empty values are returned untouched (nothing to toggle).
    """
    base = strip_context_suffix(value)
    if not base:
        return base
    return base + CONTEXT_1M_SUFFIX if enabled else base


def is_preset_model(value: Optional[str]) -> bool:
    """True when ``value`` maps to one of the selectable presets (1M variants included)."""
    return strip_context_suffix(value) in _BY_VALUE


def get_model(value: Optional[str]) -> Optional[ModelOption]:
    """Look up the base ``ModelOption`` for a stored value, honouring legacy
    slugs and the ``[1m]`` suffix."""
    return _BY_VALUE.get(strip_context_suffix(value))


def supports_1m_context(value: Optional[str]) -> bool:
    """True when the model's base is a registry model capable of a 1M context window."""
    match = get_model(value)
    return match is not None and match.supports_1m


def get_model_label(value: Optional[str]) -> str:
    """Human-readable label; falls back to the raw value, then 'Default'.

    1M variants get a ``" 1M"`` suffix on the base label.
    """
    match = get_model(value)
    if match is not None:
        return f"{match.label} 1M" if has_1m_context(value) else match.label
    return value if value else "Default"


def get_model_family(value: Optional[str]) -> Optional[ModelFamily]:
    """Resolved family, or ``None`` for unknown/custom values."""
    match = get_model(value)
    return match.family if match is not None else None


def get_context_window(value: Optional[str]) -> int:
    """Context window for the given model; 1M for ``[1m]`` variants, else 200k."""
    if has_1m_context(value):
        return CONTEXT_1M_WINDOW
    match = get_model(value)
    return match.context_window if match is not None else 200_000


def model_values() -> List[str]:
    """All selectable model IDs (base + 1M variants), for use in Pydantic
    ``Literal`` / JSON-schema enums."""
    values: List[str] = []
    for m in MODELS:
        values.append(m.value)
        if m.supports_1m:
            values.append(m.value + CONTEXT_1M_SUFFIX)
    return values


def registry_payload() -> Dict[str, object]:
    """Serialized registry for the ``GET /api/models`` endpoint."""
    return {
        "models": [
            {
                "value": m.value,
                "label": m.label,
                "family": m.family,
                "context_window": m.context_window,
                "supports_1m": m.supports_1m,
            }
            for m in MODELS
        ],
        "default": DEFAULT_MODEL,
        "inherit_value": INHERIT_VALUE,
        "context_1m_suffix": CONTEXT_1M_SUFFIX,
        "legacy_map": dict(LEGACY_MODEL_MAP),
    }
