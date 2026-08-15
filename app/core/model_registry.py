"""
Claude model registry — single source of truth for the backend.

Add/remove a model here and every Pydantic default, MCP tool enum, and
model-family check across the backend picks it up automatically.
Callers should never hardcode a model ID inline — import from this module.

Context windows are the models' NATIVE windows: the current Claude slugs
(sonnet/opus/fable) run 1M by default and there is no ``[1m]`` opt-in suffix
any more — see ``LEGACY_MODEL_MAP`` / ``_LEGACY_1M_SUFFIX`` for how stored
legacy ids are normalized.

Mirrors ``frontend/src/lib/constants/models.ts``. The two registries must
stay in sync; the frontend can hydrate from ``GET /api/models`` to verify
at runtime. Any addition/removal should land in both files in the same
commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.core.model_catalog_presets import preset_context_window, preset_kind_for

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
    """Maximum input context in tokens, as the model natively runs.

    The current Claude slugs (sonnet/opus/fable) run the 1M window by DEFAULT —
    there is no opt-in toggle any more (the old ``[1m]`` suffix mechanism is
    retired). Haiku stays on the standard 200k window."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order here is the order shown in every dropdown in the UI and the order
# reported by ``GET /api/models``.
MODELS: Tuple[ModelOption, ...] = (
    ModelOption("haiku",  "Haiku",  "haiku",     200_000),
    ModelOption("sonnet", "Sonnet", "sonnet",  1_000_000),
    ModelOption("opus",   "Opus",   "opus",    1_000_000),
    ModelOption("fable",  "Fable",  "fable",   1_000_000),
)

DEFAULT_MODEL = "sonnet"
"""Default model used when nothing is configured yet."""

INHERIT_VALUE = ""
"""Sentinel meaning 'inherit from parent' (used by subagents)."""

# Older user data stored full model IDs before the presets moved to Agent SDK
# shorthand aliases. Map those to the closest shorthand so older
# profiles/subagents round-trip cleanly. We treat these as read-only aliases —
# storage is rewritten on the next save, never eagerly. Explicit [1m] entries
# are required because normalize_model does an exact-string lookup; they now
# resolve to the BASE slug (the 1M window is the Claude default, so the suffix
# no longer selects anything).
LEGACY_MODEL_MAP: Dict[str, str] = {
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4-6[1m]": "sonnet",
    "claude-sonnet-5": "sonnet",
    "claude-sonnet-5[1m]": "sonnet",
    "claude-fable-5": "fable",
    "claude-fable-5[1m]": "fable",
    "claude-opus-4-6": "opus",
    "claude-opus-4-6[1m]": "opus",
    "claude-opus-4-7": "opus",
    "claude-opus-4-7[1m]": "opus",
    "claude-opus-4-8": "opus",
    "claude-opus-4-8[1m]": "opus",
}

# RETIRED MECHANISM — legacy-data normalizer only. The 1M context window used to
# be an opt-in capability toggled by appending this suffix to a stored model ID.
# Current Claude slugs run 1M natively, so the toggle is gone from every UI,
# schema and env path. Persisted profiles/subagents can still hold e.g.
# ``"opus[1m]"``, so ``normalize_model`` strips a trailing suffix to reach the
# base slug. Do NOT reintroduce this as a public capability helper.
_LEGACY_1M_SUFFIX = "[1m]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BY_VALUE: Dict[str, ModelOption] = {m.value: m for m in MODELS}


def normalize_model(value: Optional[str]) -> str:
    """Resolve a stored model string to its canonical form.

    - ``None`` / empty → ``""`` (inherit / unset)
    - Legacy full model ID → shorthand alias
    - A trailing legacy ``[1m]`` suffix → stripped (retired mechanism; see
      ``_LEGACY_1M_SUFFIX``)
    - Any other string → returned as-is (custom models pass through).
    """
    if not value:
        return INHERIT_VALUE
    mapped = LEGACY_MODEL_MAP.get(value)
    if mapped is not None:
        return mapped
    if value.endswith(_LEGACY_1M_SUFFIX):
        return value[: -len(_LEGACY_1M_SUFFIX)]
    return value


def is_preset_model(value: Optional[str]) -> bool:
    """True when ``value`` maps to one of the selectable presets."""
    return normalize_model(value) in _BY_VALUE


def get_model(value: Optional[str]) -> Optional[ModelOption]:
    """Look up the ``ModelOption`` for a stored value, honouring legacy slugs."""
    return _BY_VALUE.get(normalize_model(value))


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
    """Registered context window for the given Claude model; 200k when unknown."""
    match = get_model(value)
    return match.context_window if match is not None else 200_000


def model_values() -> List[str]:
    """All selectable model IDs, for use in Pydantic ``Literal`` / JSON-schema
    enums."""
    return [m.value for m in MODELS]


def _positive_int(value: Any) -> Optional[int]:
    """``value`` as a positive int, or ``None`` (unusable / not set)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        window = int(value)
    except (TypeError, ValueError):
        return None
    return window if window > 0 else None


def resolve_provider_context_window(
    provider_row: Optional[Dict[str, Any]],
    model_id: Optional[str],
    model_entry: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """The context window (tokens) for ONE model on a provider row, or ``None``.

    SINGLE SOURCE OF TRUTH — the registry payload (``_providers_payload``) and
    the spawn env overlay (``environment.apply_provider_env``) both resolve
    through here so what the UI displays is exactly what the CLI is pinned to.

    Resolution order, most specific first:

      1. the model's OWN ``context_window`` in ``models_json`` (an upstream
         catalog fetch carries it through, and an admin can curate it);
      2. the built-in preset roster for the row's kind
         (``model_catalog_presets.preset_context_window``) — this is what makes
         a 1M model like ``gpt-5.6-sol`` resolve correctly on a row whose column
         still says 272k;
      3. the provider row's ``context_window`` column — one value shared by
         every model on the row, hence the LAST resort.

    ``model_entry`` may be passed when the caller already holds the
    ``models_json`` dict (avoids a rescan); otherwise it is looked up by id.
    """
    if not provider_row:
        return None
    if model_entry is None and model_id:
        for entry in provider_row.get("models_json") or []:
            if isinstance(entry, dict) and entry.get("id") == model_id:
                model_entry = entry
                break
    if isinstance(model_entry, dict):
        own = _positive_int(model_entry.get("context_window"))
        if own is not None:
            return own
    from_preset = preset_context_window(preset_kind_for(provider_row), model_id)
    if from_preset is not None:
        return from_preset
    return _positive_int(provider_row.get("context_window"))


# Anthropic API model ids for the selectable presets. The registry's canonical
# stored form is the short Agent SDK alias ("opus"); the delegated leg's claude
# CLI resolves aliases itself, but fluid's native Anthropic leg sends the model
# string verbatim to the Messages API, where a bare alias 404s (and never
# matches the pricing table). Dateless ids on purpose — they track the current
# model in each family.
API_MODEL_IDS: Dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
}


def resolve_api_model_id(value: Optional[str]) -> Optional[str]:
    """Anthropic API model id for a stored model value.

    Presets — short aliases, the ``LEGACY_MODEL_MAP`` full ids, and persisted
    legacy ``[1m]``-suffixed values — resolve to the pinned dateless API id via
    ``normalize_model``. Anything else (custom models, other providers' ids)
    passes through verbatim, ``None``/``""`` included.
    """
    resolved = API_MODEL_IDS.get(normalize_model(value))
    return value if resolved is None else resolved


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
                    # Per-model context window: the model's own value, then the
                    # preset roster, then the row's column (see
                    # ``resolve_provider_context_window``). The row column alone
                    # is WRONG for a row whose models have different windows —
                    # it made a 1M model display/run at the row's stale value.
                    "context_window": resolve_provider_context_window(
                        row, model.get("id"), model_entry=model
                    ),
                    # Still per-ROW (no per-model output cap is modelled).
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
            }
            for m in MODELS
        ],
        "default": DEFAULT_MODEL,
        "inherit_value": INHERIT_VALUE,
        "legacy_map": dict(LEGACY_MODEL_MAP),
        "providers": _providers_payload(),
        "claude": _claude_payload(),
    }
