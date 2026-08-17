"""Built-in preset model catalogs for providers that can't be browsed.

Phase-2 Chunk D.2 — Codex / ChatGPT-subscription providers have NO browsable
``/models`` endpoint: the roster (and per-plan caps) is server-enforced by the
account behind the ``chatgpt-account-id`` header (F3 of
``docs/MULTI_PROVIDER_PHASE2_PLAN.md``). So instead of a network fetch we hand
the UI a preset, editable list that the admin curates into the provider's
``models_json`` exactly like an OpenRouter fetch result.

This is CONFIG-STYLE DATA, not logic: the constants below are meant to be
edited in place when the GPT lineup changes. Each roster is **version-dependent**
— this list only decides what shows up in the picker, never what the account or
key is actually allowed to run. Unchecking an entry in the UI simply drops it
from the curated set.

Roster note (``CODEX_PRESET_MODELS``): this is the **live-confirmed** ChatGPT/
Codex-subscription roster, not speculative naming. It was validated against the
real Codex backend (``GET /backend-api/codex/models?client_version=1.0.0``) on
2026-07-18 — see ``docs/CODEX_SPIKE_FINDINGS.md`` (surprise #1). The entitled
roster observed was ``gpt-5.6-sol``, ``gpt-5.6-terra``, ``gpt-5.6-luna``,
``gpt-5.5``, ``gpt-5.4``, ``gpt-5.4-mini`` plus ``codex-auto-review``. The
special-purpose ``codex-auto-review`` is **excluded** — it is not a general
chat/agent model (spike finding + plan ruling PR3). Because the roster is
server-enforced upstream, this list only curates what the picker offers.

Roster note (``OPENAI_PRESET_MODELS``): the current OpenAI *public API*
(api.openai.com) chat/agent lineup, verified against OpenAI's official docs
(``platform.openai.com/docs/models`` / pricing catalog) on 2026-07-18: the
GPT-5.6 flagship tier (``sol``/``terra``/``luna``) plus the ``gpt-5.4`` mini/
nano tier. Staleness is accepted as config-style data per the convention above —
edit in place as the public lineup settles; nothing downstream hardcodes these.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

# ChatGPT/Codex-subscription preset roster (LIVE-CONFIRMED 2026-07-18 — see module
# docstring + docs/CODEX_SPIKE_FINDINGS.md). Shape mirrors ``models_json``:
# ``[{"id": ..., "label": ...}]``. ``codex-auto-review`` deliberately excluded.
#
# An entry MAY additionally carry ``context_window`` (tokens) when the model's
# real window is known and differs from what a provider row advertises. This is
# the per-MODEL truth; the ``model_providers.context_window`` column is a single
# per-ROW value shared by every model on it, so it goes stale the moment one
# model on the row has a different window (that is exactly the ``gpt-5.6-sol``
# 1M case). Only fill this in when the value is KNOWN — an omitted key means
# "fall back to the provider row", never "assume a default".
CODEX_PRESET_MODELS: List[Dict[str, Any]] = [
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "context_window": 1_000_000},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
    {"id": "gpt-5.5", "label": "GPT-5.5"},
    {"id": "gpt-5.4", "label": "GPT-5.4"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
]

# OpenAI public-API (api.openai.com) preset roster — current flagship + mini/nano
# tier (verified 2026-07-18, see module docstring). Same ``[{"id", "label"}]``
# shape (plus the optional ``context_window``); editable config-style data.
OPENAI_PRESET_MODELS: List[Dict[str, Any]] = [
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "context_window": 1_000_000},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
    {"id": "gpt-5.4-nano", "label": "GPT-5.4 Nano"},
]

# Preset kind → roster. The kind is the normalized provider classification
# produced by :func:`preset_kind_for` (NOT the raw ``provider_type``: an
# already-enrolled Codex row is ``provider_type='custom'`` +
# ``auth_mode='chatgpt'`` and must still resolve to the Codex roster).
_PRESET_ROSTERS: Dict[str, List[Dict[str, Any]]] = {
    "chatgpt": CODEX_PRESET_MODELS,
    "openai": OPENAI_PRESET_MODELS,
}


def preset_kind_for(provider_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Normalized preset classification for a provider row, or ``None``.

    Keys on ``auth_mode`` FIRST, then ``provider_type`` (plan ruling PR1 — the
    already-enrolled Codex row is ``provider_type='custom'`` +
    ``auth_mode='chatgpt'`` and must still resolve to the Codex roster):

      * ``auth_mode == "chatgpt"``            → ``"chatgpt"`` (Codex roster)
      * ``provider_type == "chatgpt"``        → ``"chatgpt"`` (new enrollments)
      * ``provider_type == "openai"``         → ``"openai"``

    Everything else (``openrouter`` / ``custom``) is browsable upstream → ``None``.
    """
    if not provider_row:
        return None
    if provider_row.get("auth_mode") == "chatgpt":
        return "chatgpt"
    provider_type = provider_row.get("provider_type")
    if provider_type in _PRESET_ROSTERS:
        return provider_type
    return None


def preset_models_for(provider_row: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Return the preset catalog for a provider, or ``None`` if it has none.

    Preset providers carry a built-in roster instead of a browsable upstream
    ``/models`` endpoint; see :func:`preset_kind_for` for the classification.
    Returns a fresh copy so callers can mutate without touching the constant.
    """
    kind = preset_kind_for(provider_row)
    if kind is None:
        return None
    return [dict(entry) for entry in _PRESET_ROSTERS[kind]]


def preset_context_window(
    provider_kind: Optional[str], model_id: Optional[str]
) -> Optional[int]:
    """The ``context_window`` a preset roster declares for ``model_id``, or ``None``.

    ``provider_kind`` is the normalized kind from :func:`preset_kind_for`
    (``"chatgpt"`` / ``"openai"``). ``None`` means "this roster says nothing
    about that model's window" — callers fall back to the provider row.
    """
    if not provider_kind or not model_id:
        return None
    roster = _PRESET_ROSTERS.get(provider_kind)
    if not roster:
        return None
    for entry in roster:
        if entry.get("id") != model_id:
            continue
        window = entry.get("context_window")
        try:
            window = int(window)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return window if window > 0 else None
    return None
