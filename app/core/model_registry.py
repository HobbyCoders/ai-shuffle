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

import logging
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

ModelFamily = Literal["haiku", "sonnet", "opus", "fable"]


@dataclass(frozen=True)
class ModelOption:
    """A selectable Claude model and its capabilities."""

    value: str
    """Model identifier (an Agent SDK shorthand alias) persisted to storage and
    sent to the SDK."""

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
    ModelOption("haiku",  "Haiku",  "haiku",  200_000, supports_1m=False),
    ModelOption("sonnet", "Sonnet", "sonnet", 200_000, supports_1m=True),
    ModelOption("opus",   "Opus",   "opus",   200_000, supports_1m=True),
    ModelOption("fable",  "Fable",  "fable",  200_000, supports_1m=True),
)

DEFAULT_MODEL = "sonnet"
"""Default model used when nothing is configured yet."""

INHERIT_VALUE = ""
"""Sentinel meaning 'inherit from parent' (used by subagents)."""

# Older user data stored full model IDs before the presets moved to Agent SDK
# shorthand aliases. Map those to the closest shorthand so older
# profiles/subagents round-trip cleanly. We treat these as read-only aliases —
# storage is rewritten on the next save, never eagerly. Explicit [1m] entries
# are required because normalize_model does an exact-string lookup.
LEGACY_MODEL_MAP: Dict[str, str] = {
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4-6[1m]": "sonnet[1m]",
    "claude-sonnet-5": "sonnet",
    "claude-sonnet-5[1m]": "sonnet[1m]",
    "claude-fable-5": "fable",
    "claude-fable-5[1m]": "fable[1m]",
    "claude-opus-4-6": "opus",
    "claude-opus-4-6[1m]": "opus[1m]",
    "claude-opus-4-7": "opus",
    "claude-opus-4-7[1m]": "opus[1m]",
    "claude-opus-4-8": "opus",
    "claude-opus-4-8[1m]": "opus[1m]",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BY_VALUE: Dict[str, ModelOption] = {m.value: m for m in MODELS}


def normalize_model(value: Optional[str]) -> str:
    """Resolve a stored model string to its canonical form.

    - ``None`` / empty → ``""`` (inherit / unset)
    - Legacy full model ID → shorthand alias
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


def _is_subscription_row(row: Dict[str, object]) -> bool:
    """Whether a ``model_providers`` row authenticates as a ChatGPT subscription.

    Mirrors ``app.core.provider.omni_credentials._is_codex_subscription``:
    ``provider_type == "chatgpt"`` OR ``auth_mode == "chatgpt"`` (a ``custom``
    row can carry the Codex auth mode). Everything else is API-key auth.
    """
    return row.get("provider_type") == "chatgpt" or row.get("auth_mode") == "chatgpt"


def _provider_plan_types() -> Dict[str, Optional[str]]:
    """Latest known ``plan_type`` per provider id, from ``provider_usage_snapshots``.

    The table is keyed by ``provider_id`` (one row per provider), but we still
    keep the newest ``fetched_at`` defensively so a duplicate can never win by
    iteration order. Best-effort: any db problem yields ``{}`` so the registry
    payload keeps building (same degradation contract as ``_providers_payload``).
    """
    try:
        from app.db import database
    except ImportError:
        return {}
    try:
        rows = database.get_all_provider_usage_snapshots()
    except Exception as e:
        logger.warning("Could not load provider usage snapshots for plan types: %s", e)
        return {}
    latest: Dict[str, Optional[str]] = {}
    seen_at: Dict[str, int] = {}
    for row in rows:
        provider_id = row.get("provider_id")
        if not provider_id:
            continue
        fetched_at = row.get("fetched_at") or 0
        try:
            fetched_at = int(fetched_at)
        except (TypeError, ValueError):
            fetched_at = 0
        if provider_id in seen_at and seen_at[provider_id] >= fetched_at:
            continue
        seen_at[provider_id] = fetched_at
        latest[provider_id] = row.get("plan_type")
    return latest


def _providers_payload() -> List[Dict[str, object]]:
    """Serialized ``model_providers`` rows for the registry payload.

    Lazily imports the db layer (core must not import db at module load —
    that would create a core→db import cycle). Never includes the encrypted
    ``auth_token`` blob: it reads via ``get_enabled_model_providers()``, whose
    default (``include_token=False``) omits that column entirely. Degrades to
    an empty list if the db layer can't be imported or isn't initialized yet
    (early boot) — an empty table is a normal, distinct case that still
    returns ``[]`` from the query itself.
    """
    try:
        from app.db import database
    except ImportError:
        return []
    try:
        rows = database.get_enabled_model_providers()
    except Exception as e:
        # Degrade gracefully rather than failing the whole registry payload
        # (which would 500 GET /api/models app-wide). Covers DB pool not yet
        # initialized (early boot RuntimeError), a rolling-deploy missing table
        # (psycopg2 ProgrammingError), and pool exhaustion (PoolError). A normal
        # empty table is not this branch: the query itself returns [].
        logger.warning("Could not load model providers for registry payload: %s", e)
        return []
    # Plan strings are only meaningful for subscription rows, and only fetched
    # when at least one such row exists (an all-API-key install skips the read).
    plan_types = _provider_plan_types() if any(_is_subscription_row(r) for r in rows) else {}
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "provider_type": row["provider_type"],
            # Display metadata (brand + auth-kind subtext). ``auth_kind`` says
            # HOW the row authenticates; ``plan_type`` names the subscription
            # tier for subscription rows (null for API-key rows, and for a
            # subscription row whose usage snapshot hasn't landed yet).
            "auth_kind": "subscription" if _is_subscription_row(row) else "api",
            "plan_type": (
                plan_types.get(row["id"]) if _is_subscription_row(row) else None
            ),
            "models": [
                {
                    "id": model.get("id"),
                    "label": model.get("label"),
                    # Additive per-model metadata (Chunk D.4). Only what the
                    # provider row itself carries — one value shared by all of
                    # that provider's models (no per-model store, no models.dev).
                    "context_window": row.get("context_window"),
                    "max_output_tokens": row.get("max_output_tokens"),
                }
                for model in (row.get("models_json") or [])
            ],
            "default_model": row.get("default_model"),
        }
        for row in rows
    ]


def _claude_payload() -> Dict[str, object]:
    """Display metadata for NATIVE Claude — how this install authenticates.

    Mirrors ``_providers_payload``'s ``auth_kind``/``plan_type`` pair for the
    Anthropic side, which has no ``model_providers`` row to carry them:

      * a stored ``anthropic_api_key`` wins over any connected account (see
        ``environment.inject_claude_code_auth``) → ``auth_kind = "api"``, no plan;
      * otherwise the connected OAuth account is used → ``auth_kind =
        "subscription"`` with the account's ``subscription_type`` ("max"/"pro")
        when known, else null.

    Lazily imported and fully degrading: ANY failure yields nulls rather than
    500-ing ``GET /api/models`` app-wide (same contract as ``_providers_payload``).
    """
    try:
        from app.api.settings_crypto import get_decrypted_api_key

        if get_decrypted_api_key("anthropic_api_key"):
            return {"auth_kind": "api", "plan_type": None}

        from app.core import claude_accounts
        from app.db import database

        account_id = claude_accounts.get_active_account_id()
        plan = None
        if account_id:
            row = database.get_claude_account(account_id)
            if row:
                plan = row.get("subscription_type")
        return {"auth_kind": "subscription", "plan_type": plan}
    except Exception as e:  # noqa: BLE001 — display metadata is never fatal
        logger.warning("Could not resolve Claude auth metadata for registry: %s", e)
        return {"auth_kind": None, "plan_type": None}


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
        "providers": _providers_payload(),
        "claude": _claude_payload(),
    }
