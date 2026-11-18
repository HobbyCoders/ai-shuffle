"""Per-chat speed tier: Standard, Fast or Ultrafast.

A chat's speed is stored as two values so workers and apps that only know Fast
mode keep working:

* ``fast`` (bool, unchanged since v81): on for Fast and for Ultrafast.
* ``speed`` (text, nullable): the tier the owner picked, one of :data:`SPEEDS`.

``fast`` decides on or off; ``speed`` only refines an on chat. So a chat an
older peer or the Android app turned off is Standard whatever ``speed`` still
says, and an older peer that reads only ``fast`` runs an Ultrafast chat on Fast
instead of failing. Every writer in this build stores an agreeing pair
(:func:`stored_pair`).

On the wire (OpenAI Responses, HTTP or WebSocket) Fast is ``service_tier:
"priority"`` and Ultrafast is ``service_tier: "ultrafast"``; Standard sends no
``service_tier``. Ultrafast exists only on OpenAI's Responses lane, for an
OpenAI API-key row on the documented models, and for a ChatGPT sign-in whose
own Codex model catalog lists the ``ultrafast`` service tier for the model
(OpenAI gates it by plan there, so a Plus account never sees it).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

STANDARD = "standard"
FAST = "fast"
ULTRAFAST = "ultrafast"
SPEEDS: Tuple[str, ...] = (STANDARD, FAST, ULTRAFAST)

#: ``service_tier`` request values per speed (Standard sends none).
SERVICE_TIERS: Dict[str, str] = {FAST: "priority", ULTRAFAST: "ultrafast"}

#: Catalog tier ids worth keeping from a Codex ``/models`` entry. Anything else
#: a catalog lists is dropped, so the stored row never carries arbitrary text.
KNOWN_SERVICE_TIERS: Tuple[str, ...] = ("priority", "flex", "ultrafast")

# The models OpenAI documents for Ultrafast on the API
# (developers.openai.com/api/docs/guides/ultrafast-mode), exact ids only: no
# snapshot or other GPT-6 variant is documented to have it.
_ULTRAFAST_API_MODELS = frozenset({"gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.1-sol"})


def normalize_speed(value: Any) -> Optional[str]:
    """``value`` as a known speed, else None."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in SPEEDS:
            return value
    return None


def resolve_speed(fast: Any, speed: Any = None) -> str:
    """The tier a chat runs on from its stored ``fast`` and ``speed``.

    ``fast`` off is Standard whatever ``speed`` says (an older writer turned it
    off); ``fast`` on is Ultrafast only when ``speed`` says so, else Fast.
    """
    if not fast:
        return STANDARD
    return ULTRAFAST if normalize_speed(speed) == ULTRAFAST else FAST


def speed_fields(speed: str) -> Dict[str, Any]:
    """The ``fast``/``speed`` pair every writer stores for ``speed``."""
    speed = normalize_speed(speed) or STANDARD
    return {"fast": speed != STANDARD, "speed": speed}


def stored_pair(fast: Any = None, speed: Any = None, *,
                prior: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The ``fast``/``speed`` pair to store; every writer of the pair uses it.

    The stored pair always agrees: ``fast`` off has speed ``standard``,
    ``fast`` on has ``fast`` or ``ultrafast`` (NULL only on rows never written
    by this build, read as :func:`resolve_speed` says).

    * A known ``speed`` is the pick, and ``fast`` follows it.
    * ``fast`` alone (an older client, peer or parent): off is Standard; on is
      Ultrafast only when ``prior`` (the stored pair it replaces) runs
      Ultrafast, else Fast.
    * Neither, with ``prior``: a stored pair being copied (fork, clone,
      mirror), carried over at the tier it runs.
    * Neither and no ``prior``: nothing was picked (both None).
    """
    tier = normalize_speed(speed)
    if tier is None and fast is None and prior is not None:
        fast = prior.get("fast")
    if tier is None:
        if fast is None:
            return {"fast": None, "speed": None}
        kept = prior is not None and resolve_speed(prior.get("fast"), prior.get("speed")) == ULTRAFAST
        tier = (ULTRAFAST if kept else FAST) if fast else STANDARD
    return speed_fields(tier)


def service_tier_for(speed: Any) -> Optional[str]:
    """The ``service_tier`` request value for a speed (None for Standard)."""
    return SERVICE_TIERS.get(normalize_speed(speed) or STANDARD)


def options_speed(options: Any, *, prefix: str = "") -> str:
    """The tier an options object carries (``fast``/``speed`` or ``aih_`` names)."""
    return resolve_speed(getattr(options, prefix + "fast", None),
                         getattr(options, prefix + "speed", None))


# ---------------------------------------------------------------------------
# Catalog tiers
# ---------------------------------------------------------------------------

def sanitize_service_tiers(entry: Mapping[str, Any]) -> Optional[List[str]]:
    """The known service tier ids a Codex catalog entry lists, or None.

    Reads ``service_tiers`` (``[{id, name, description}]``) and the deprecated
    ``additional_speed_tiers`` (``["fast"]``, which Codex maps to
    ``priority``). None when the entry has neither field, so an older catalog
    is told apart from one that lists no tiers (``[]``).
    """
    if not isinstance(entry, Mapping):
        return None
    tiers = entry.get("service_tiers")
    legacy = entry.get("additional_speed_tiers")
    if not isinstance(tiers, list) and not isinstance(legacy, list):
        return None
    found = set()
    for item in tiers if isinstance(tiers, list) else ():
        tier_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(tier_id, str):
            found.add(tier_id.strip().lower())
    for item in legacy if isinstance(legacy, list) else ():
        if isinstance(item, str) and item.strip().lower() == "fast":
            found.add("priority")
    return [tier for tier in KNOWN_SERVICE_TIERS if tier in found]


def stored_service_tiers(entry: Any) -> Optional[List[str]]:
    """A saved model entry's ``service_tiers``, re-sanitized; None when absent."""
    if not isinstance(entry, Mapping):
        return None
    tiers = entry.get("service_tiers")
    if not isinstance(tiers, list):
        return None
    return [tier for tier in KNOWN_SERVICE_TIERS
            if tier in {t for t in tiers if isinstance(t, str)}]


def merge_service_tiers(models: Iterable[Any], catalog: Iterable[Mapping[str, Any]]) -> List[Any]:
    """Saved model entries with ``service_tiers`` set from a fresh catalog only.

    A successful catalog fetch is the only source of tiers, so it replaces
    them on every saved model: one it lists takes the tiers listed there, and
    one it omits, or lists without tiers, loses any it had (a plan change can
    take Ultrafast away). Catalogs without tiers (every non-ChatGPT row) leave
    no tiers at all.
    """
    fresh = {}
    for entry in catalog or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            fresh[entry["id"]] = stored_service_tiers(entry)
    merged: List[Any] = []
    for entry in models or ():
        if not isinstance(entry, Mapping):
            merged.append(entry)
            continue
        updated = {k: v for k, v in entry.items() if k != "service_tiers"}
        tiers = fresh.get(entry.get("id"))
        if tiers is not None:
            updated["service_tiers"] = tiers
        merged.append(updated)
    return merged


# ---------------------------------------------------------------------------
# Who may run Ultrafast
# ---------------------------------------------------------------------------

def _row_kind(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    from app.core.model_catalog_presets import preset_kind_for

    return preset_kind_for(row) if row else None


def _row_model_entry(row: Mapping[str, Any], model_id: str) -> Optional[Mapping[str, Any]]:
    for entry in row.get("models_json") or []:
        if isinstance(entry, Mapping) and entry.get("id") == model_id:
            return entry
    return None


def ultrafast_supported_for_row(row: Optional[Mapping[str, Any]], model_id: Optional[str],
                                model_entry: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether ``model_id`` on provider ``row`` can run Ultrafast.

    * A ChatGPT sign-in (``chatgpt`` row, or a ``custom`` row signed in with
      ChatGPT): only when that row's saved Codex catalog lists ``ultrafast``
      among the model's service tiers.
    * An OpenAI API-key row: the models OpenAI documents for Ultrafast.
    * Every other row, and no row: never.
    """
    model_id = (model_id or "").strip()
    if not row or not model_id:
        return False
    kind = _row_kind(row)
    if kind == "chatgpt":
        entry = model_entry if model_entry is not None else _row_model_entry(row, model_id)
        return ULTRAFAST in (stored_service_tiers(entry) or [])
    if kind == "openai":
        return model_id in _ULTRAFAST_API_MODELS
    return False


__all__ = [
    "STANDARD", "FAST", "ULTRAFAST", "SPEEDS", "SERVICE_TIERS", "KNOWN_SERVICE_TIERS",
    "normalize_speed", "resolve_speed", "speed_fields", "stored_pair", "service_tier_for",
    "options_speed", "sanitize_service_tiers", "stored_service_tiers", "merge_service_tiers",
    "ultrafast_supported_for_row",
]
