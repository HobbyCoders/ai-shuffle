"""Built-in preset model catalogs for providers that can't be browsed.

Phase-2 Chunk D.2 — Codex / ChatGPT-subscription providers have NO browsable
``/models`` endpoint: the roster (and per-plan caps) is server-enforced by the
account behind the ``chatgpt-account-id`` header (F3 of
``docs/MULTI_PROVIDER_PHASE2_PLAN.md``). So instead of a network fetch we hand
the UI a preset, editable list that the admin curates into the provider's
``models_json`` exactly like an OpenRouter fetch result.

This is CONFIG-STYLE DATA, not logic: the constant below is meant to be edited
in place when the GPT lineup changes. The roster is **version-dependent** and
ultimately **server-enforced upstream** — this list only decides what shows up
in the picker, never what the account is actually allowed to run. Unchecking an
entry in the UI simply drops it from the curated set.

Roster note: the plan's F3 finding names the "GPT-5.6" lineup (GA 2026-07-09)
but does not enumerate concrete model ids, so the ids below are the conventional
Codex names (``gpt-5.6`` / ``gpt-5.6-codex`` / ``gpt-5.6-codex-mini``). Edit
freely as OpenAI's naming settles — nothing downstream hardcodes these values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ChatGPT/Codex-subscription preset roster. Shape mirrors ``models_json``:
# ``[{"id": ..., "label": ...}]``. Editable — see module docstring.
CODEX_PRESET_MODELS: List[Dict[str, str]] = [
    {"id": "gpt-5.6", "label": "GPT-5.6"},
    {"id": "gpt-5.6-codex", "label": "GPT-5.6 Codex"},
    {"id": "gpt-5.6-codex-mini", "label": "GPT-5.6 Codex Mini"},
]


def preset_models_for(provider_row: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, str]]]:
    """Return the preset catalog for a provider, or ``None`` if it has none.

    Only ChatGPT/Codex-subscription providers (``auth_mode == "chatgpt"``) carry
    a preset; everything else is browsable via the upstream ``/models`` fetch.
    Returns a fresh copy so callers can mutate without touching the constant.
    """
    if not provider_row:
        return None
    if provider_row.get("auth_mode") == "chatgpt":
        return [dict(entry) for entry in CODEX_PRESET_MODELS]
    return None
