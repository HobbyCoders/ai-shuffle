"""A model the user asked for, on one StartSession or Task start (#1830).

An agent may pass ``model`` to run one new chat or subagent on a model other
than the one it would get anyway (the agent's pin, else the owner's subagent
default; for a new chat, the caller's own model). Any model this install can
run is eligible: a Claude registry alias or a model on an enabled provider's
curated list, in the app's id format (``sonnet``, ``prov:<providerId>:<modelId>``).

The model the agent "feels like" is never used on its word. A different model
runs only when the user asked for it, checked here by the host:

1. **Named by the user.** :func:`user_texts` collects what the user typed in
   the calling chat since (and including) their last message: the last turn
   with source ``user``/``queue`` and every later message steered into it. A
   Task worker is followed up to its top-level chat, and a chat another chat
   started with StartSession to the user message that began that chain.
   Peer messages (``agent_message`` turns), tool output, notices and model
   text are never turns of those sources, so they never count.
   :func:`named_by_user` then looks for the model's id, alias or display
   name, case-insensitively with separators ignored ("GPT-6 Astra",
   "gpt 6 astra", "gpt6astra"), or for one distinctive word of its name
   ("astra") when no other eligible model shares that word.
2. **Approved by the owner.** Otherwise :func:`confirm` raises the ordinary
   permission prompt ("Start ... on GPT-6 Astra instead of Opus?"), in every
   permission mode, Bypass included, and never from a saved rule. Away mode,
   a denial or no answer refuses the start; nothing falls back to the
   requested model or to the session's model.

The choice applies to that one start. It never changes an agent's pin or the
subagent default.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# How the prompt and the receipts name the two ways a model is accepted.
NAMED = "named_by_user"
APPROVED = "approved_by_owner"

# The pseudo-tool the owner's prompt is raised under. Its own name keeps a
# remembered "always" answer on this prompt from ever approving the
# StartSession or Task tool itself; the prompt skips saved rules anyway.
PROMPT_TOOL = "ModelSwitch"

# Words too generic to stand for one model on their own, even when only one
# eligible model happens to contain them.
_GENERIC_WORDS = frozenset({
    "claude", "gpt", "grok", "gemini", "llama", "model", "models", "chat", "mini",
    "nano", "pro", "max", "preview", "latest", "turbo", "flash", "lite", "fast",
    "instruct", "large", "small", "medium", "high", "low", "thinking", "reasoning",
    "beta", "free", "plus", "ultra", "code", "coder", "vision", "online", "exp",
    "experimental", "new", "the", "base", "open", "auto", "default", "standard",
    "anthropic", "openai", "google", "meta", "xai", "mistral", "deepseek", "qwen",
})
_CHAIN_LIMIT = 16


@dataclass(frozen=True)
class Choice:
    """An eligible model: its app id, provider (None = Claude) and bare id."""
    model: str
    provider_id: Optional[str]
    model_id: str


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def app_id(provider_id: Optional[str], model_id: Optional[str]) -> Optional[str]:
    """The app-format id for a model on a leg (None = Claude), or None."""
    text = (model_id or "").strip()
    if not text:
        return None
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(text)
    if decoded is not None:
        return f"prov:{decoded[0]}:{decoded[1]}"
    if provider_id:
        return f"prov:{provider_id}:{text}"
    if is_native_claude_model(text):
        from app.core.model_registry import normalize_model

        return normalize_model(text)
    return text


def canonical(value: str, leg: Optional[str] = None) -> str:
    """``value`` in app-format, without asking whether it runs here.

    A ``prov:`` composite and a Claude name stand for themselves; any other
    bare id is read as a model of ``leg`` (the caller's or target's provider),
    the way Task has always read a bare id.
    """
    from app.core.query_options import is_native_claude_model

    text = value.strip()
    if text.startswith("prov:") or is_native_claude_model(text) or not leg:
        return app_id(None, text) or text
    return f"prov:{leg}:{text}"


def resolve(value: Any, leg: Optional[str] = None, *, runs_here: bool = True) -> Choice:
    """The eligible model ``value`` names, or ``ValueError`` saying why not.

    ``runs_here``: the model must run on this worker (enabled provider whose
    list holds it, with credentials). A start on another worker is checked
    by that worker instead.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("model must be a non-empty model id of at most 300 characters.")
    from app.core.query_options import decode_provider_model

    wanted = canonical(value, leg)
    if runs_here:
        from app.core import subagent_default

        normalized, error = subagent_default.normalize_model(wanted)
        if error or not normalized:
            raise ValueError(f"Model '{value.strip()}' cannot run on this worker: {error}")
        wanted = normalized
    elif not wanted.startswith("prov:"):
        from app.core.query_options import is_native_claude_model

        if not is_native_claude_model(wanted):
            raise ValueError(
                f"'{value.strip()}' is not a Claude model. A provider's model is written "
                "prov:<providerId>:<modelId>.")
    decoded = decode_provider_model(wanted)
    provider_id, model_id = (decoded[0], decoded[1]) if decoded else (None, wanted)
    if runs_here and provider_id:
        from app.core.task_dispatch import _credential_gap

        gap = _credential_gap(provider_id)
        if gap:
            raise ValueError(gap.replace("the worker would", "it would"))
    return Choice(model=wanted, provider_id=provider_id, model_id=model_id)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _tokens(text: Any) -> List[str]:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()


def _provider_rows() -> List[Dict[str, Any]]:
    try:
        from app.db.model_providers import get_enabled_model_providers

        return list(get_enabled_model_providers() or [])
    except Exception:  # noqa: BLE001 — names fall back to the id alone
        logger.debug("Could not list providers for model names", exc_info=True)
        return []


def _entries(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    import json

    models = row.get("models_json")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except ValueError:
            models = None
    return [entry for entry in models or [] if isinstance(entry, dict) and entry.get("id")]


def _catalog() -> Dict[str, Tuple[str, ...]]:
    """Every eligible model's app id and the names it goes by."""
    from app.core.model_registry import API_MODEL_IDS, MODELS

    names: Dict[str, Tuple[str, ...]] = {}
    for option in MODELS:
        names[option.value] = tuple(filter(None, (
            option.value, option.label, f"claude {option.value}", API_MODEL_IDS.get(option.value))))
    for row in _provider_rows():
        for entry in _entries(row):
            model_id = str(entry["id"])
            spelled = [model_id, model_id.rsplit("/", 1)[-1]]
            for key in ("label", "name", "display_name"):
                if isinstance(entry.get(key), str) and entry[key].strip():
                    spelled.append(entry[key])
            names[f"prov:{row.get('id')}:{model_id}"] = tuple(dict.fromkeys(spelled))
    return names


def names_for(model: str, catalog: Optional[Dict[str, Tuple[str, ...]]] = None) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(full names, distinctive words)`` the user may call ``model`` by."""
    catalog = _catalog() if catalog is None else catalog
    from app.core.query_options import decode_provider_model

    decoded = decode_provider_model(model)
    full = catalog.get(model) or ((decoded[1], decoded[1].rsplit("/", 1)[-1]) if decoded else (model,))
    others = set()
    for other, spelled in catalog.items():
        if other != model:
            for name in spelled:
                others.update(_tokens(name))
    words = []
    for name in full:
        for token in _tokens(name):
            if (token.isalpha() and len(token) >= 3 and token not in _GENERIC_WORDS
                    and token not in others and token not in words):
                words.append(token)
    return tuple(full), tuple(words)


def _mentions(text_tokens: Sequence[str], name: str) -> bool:
    """Whether ``name`` (separators ignored) spans whole words of the text."""
    target = "".join(_tokens(name))
    if not target:
        return False
    for start in range(len(text_tokens)):
        joined = ""
        for token in text_tokens[start:]:
            joined += token
            if len(joined) >= len(target):
                break
        if joined == target:
            return True
    return False


def named_by_user(model: str, texts: Iterable[str],
                  catalog: Optional[Dict[str, Tuple[str, ...]]] = None) -> bool:
    """Whether any of ``texts`` names ``model``."""
    full, words = names_for(model, catalog)
    for text in texts:
        tokens = _tokens(text)
        if any(_mentions(tokens, name) for name in full) or any(word in tokens for word in words):
            return True
    return False


def label(model: Optional[str]) -> str:
    """A short human name: "GPT-6 Astra (Lane)" or "Opus"."""
    if not model:
        return "no model"
    from app.core.query_options import decode_provider_model

    decoded = decode_provider_model(model)
    if decoded is None:
        from app.core.model_registry import get_model

        option = get_model(model)
        return option.label if option is not None else model
    for row in _provider_rows():
        if row.get("id") == decoded[0]:
            entry = next((e for e in _entries(row) if e.get("id") == decoded[1]), {})
            name = entry.get("label") if isinstance(entry.get("label"), str) else decoded[1]
            return f"{name} ({row.get('name') or decoded[0]})"
    return f"{decoded[1]} ({decoded[0]})"


def provider_label(provider_id: Optional[str]) -> str:
    from app.core.agent_provider_models import task_model_choices

    return task_model_choices(provider_id or None)[0]


# ---------------------------------------------------------------------------
# What the user typed
# ---------------------------------------------------------------------------

def _user_prompts_since(cursor, session_id: str, position: Optional[int] = None) -> List[str]:
    """The user's messages in ``session_id`` from ``position`` on, else from
    their last message on (that message plus everything steered after it)."""
    if position is None:
        cursor.execute(
            "SELECT position FROM turns WHERE session_id=%s AND source IN ('user','queue') "
            "AND state <> 'queued' AND COALESCE(stop_reason, '') <> 'steered' "
            "ORDER BY position DESC LIMIT 1", (session_id,))
        row = cursor.fetchone()
        if not row:
            return []
        position = row["position"]
    cursor.execute(
        "SELECT prompt FROM turns WHERE session_id=%s AND source IN ('user','queue') "
        "AND state <> 'queued' AND position >= %s ORDER BY position", (session_id, position))
    return [row["prompt"] for row in cursor.fetchall() if isinstance(row.get("prompt"), str)]


def _context_turn(session_id: str) -> Optional[Dict[str, Any]]:
    from app.core import turn_scheduler
    from app.db import turns

    return turns.get_live_turn(session_id) or turn_scheduler._latest_context_turn(session_id)


def user_texts(session_id: str) -> List[str]:
    """What the user typed that this start may act on (see module doc). Never raises."""
    try:
        from app.core.task_dispatch import caller_chain
        from app.db.base import get_db

        _, root, _ = caller_chain(session_id)
        current = root
        seen = {current}
        with get_db() as conn:
            cursor = conn.cursor()
            texts: List[str] = _user_prompts_since(cursor, current)
            for _ in range(_CHAIN_LIMIT):
                # A chat StartSession began: follow its chain to the chat (and
                # the user turn) it started from.
                context = _context_turn(current) or {}
                origin_id = (context.get("payload") or {}).get("orchestration_root")
                if not origin_id or origin_id == context.get("id"):
                    break
                cursor.execute("SELECT session_id, source, position FROM turns WHERE id=%s", (origin_id,))
                origin = cursor.fetchone()
                if not origin or origin["session_id"] in seen:
                    break  # begun on another worker (its messages are not here), or a loop
                current = origin["session_id"]
                seen.add(current)
                from_user = origin["source"] in ("user", "queue")
                texts.extend(_user_prompts_since(cursor, current, origin["position"] if from_user else None))
        return texts
    except Exception:  # noqa: BLE001 — no evidence means the owner is asked
        logger.warning("Could not read the user's messages for %s", session_id, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def _refusal(model: str, baseline: Optional[str], why: str) -> str:
    instead = label(baseline) if baseline else "the default"
    return (
        f"Not started on {label(model)}: {why} Running on a model other than {instead} "
        "needs the user to ask for that model. Omit model to use "
        + (f"{instead} (the agent's own model, else the subagent default)." if baseline
           else "the agent's own model or the subagent default.")
        + " Never retry with a different model the user did not name."
    )


async def confirm(session_id: str, profile_id: Optional[str], *, model: str,
                  baseline: Optional[str], action: str) -> str:
    """How the user asked for ``model``: :data:`NAMED` or :data:`APPROVED`.

    ``action`` completes "Do you want me to ..." in the owner's prompt, e.g.
    "start the chat “Review” on GPT-6 Astra (Lane) instead of Opus".
    Raises ``PermissionError`` with the refusal when the user did not ask.
    """
    import asyncio

    texts = await asyncio.to_thread(user_texts, session_id)
    if texts and await asyncio.to_thread(named_by_user, model, texts):
        return NAMED
    from app.core import provider_agent_approvals
    from app.core.agent_types import PermissionResultAllow
    from app.core.permission_handler import permission_handler

    async def _no_origin_socket(_event: Dict[str, Any]) -> None:
        return None  # request_permission fans the prompt out to the owner's devices

    question = action[0].upper() + action[1:] + "?"
    kwargs = {
        "request_id": f"model-switch-{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "profile_id": profile_id or "",
        "tool_name": PROMPT_TOOL,
        "tool_input": {"model": model, "instead_of": baseline, "model_switch": action},
        "broadcast_func": _no_origin_socket,
        "title": question,
        "display_name": "Switch model",
        "description": question,
        "decision_reason": "The agent asked for a model you did not name in this chat.",
        "skip_saved_rules": True,
    }
    try:
        result = await provider_agent_approvals.route_permission(kwargs, permission_handler.request_permission)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a failed prompt never switches
        logger.exception("Model switch prompt failed for %s", session_id)
        result = None
    if isinstance(result, PermissionResultAllow) or getattr(result, "behavior", None) == "allow":
        return APPROVED
    message = str(getattr(result, "message", "") or "")
    if message.startswith("Owner away") or "away" in message.casefold():
        why = "the owner is away, so the switch was not approved."
    elif "timed out" in message:
        why = "the owner did not answer the prompt."
    elif result is None:
        why = "the owner could not be asked."
    else:
        why = "the owner declined the switch."
    raise PermissionError(_refusal(model, baseline, f"the user did not name it in this chat and {why}"))


# ---------------------------------------------------------------------------
# Run settings
# ---------------------------------------------------------------------------

def settings_for_model(model: str, provider_id: Optional[str], *, effort: Any = None,
                       context_window: Any = None, fast: Any = None) -> Dict[str, Any]:
    """The inherited run settings that still apply to ``model``.

    An effort, context window or fast mode the new model does not support is
    dropped (None), so it starts on that model's own default instead of a
    setting its provider would reject.
    """
    from app.core.model_effort import normalize_effort
    from app.core.model_registry import context_window_choices
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.core.query_options import decode_provider_model
    from app.db.model_providers import get_model_provider

    decoded = decode_provider_model(model)
    bare = decoded[1] if decoded else model
    provider = get_model_provider(provider_id) if provider_id else None
    route = _decide_provider_kind(SessionOptions(
        model=bare, extra={"provider_id": provider_id} if provider_id else {}))
    return {
        "effort": normalize_effort(effort, bare, provider, route=route) if effort else None,
        "context_window": (context_window if context_window
                           and context_window in context_window_choices(bare, provider) else None),
        "fast": fast if not fast or route in ("delegated", "openai_responses") else None,
    }
