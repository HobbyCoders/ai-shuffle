"""A model the user asked for, on one StartSession or Task start (#1830).

An agent may pass ``model`` to run one new chat or subagent on a model other
than the one it would get anyway (the agent's pin, else the owner's subagent
default; for a new chat, the caller's own model). Any model this install can
run is eligible: a Claude registry alias or a model on an enabled provider's
curated list, in the app's id format (``sonnet``, ``prov:<providerId>:<modelId>``).

The model the agent "feels like" is never used on its word. A different model
runs only when the user asked for it, checked here by the host:

1. **Named by the user.** :func:`user_texts` collects every message the user
   typed in the calling chat (:data:`USER_SOURCES`: ``user``, ``queue``, the
   first message of a chat started on a new branch, ``worktree``, and an HTTP
   API message, ``api``), so a standing instruction such as "spin up Opus
   sub-agents" keeps counting after later messages. A Task worker is followed
   up to its top-level chat, and a chat another chat started with
   StartSession to every owner-typed message in its originating chats on this
   worker (up to 16 real launch links, stopping at a loop, a missing origin
   or a different principal). Ordinary MessageSession context is not ancestry.
   The sending principal must own the chat, using api_user_scope's legacy
   worker inheritance. The fast history window is newest-first, at most 512
   rows and 262144 history characters across the chain. On a miss, a five-second
   paged search checks older messages and saved naming in launch ancestors;
   oversized fallback messages are skipped. Successful naming
   is saved as an exact chat/model/principal receipt with its own-message
   provenance, so later starts need no rescan. Naming another model keeps both
   acceptable; revocation is not parsed. Peer messages
   (``agent_message`` turns), connector channels, tool output, notices and
   model text are never turns of those sources, so they never count. Two
   turns of those sources hold text the user did not write and are read
   narrower: an answer to the agent's question counts its answer, not the
   question, and a voice call's hand-off (written by the voice model) does
   not count.
   :func:`named_by_user` then looks for the model's id, alias or display
   name, case-insensitively with separators ignored ("GPT-6 Astra",
   "gpt 6 astra", "gpt6astra"), or for one distinctive word of its name
   ("astra") when no other eligible model shares that word.
2. **Approved by the owner.** Otherwise :func:`confirm` raises the ordinary
   permission prompt ("Start ... on GPT-6 Astra instead of Opus?"), in every
   permission mode, Bypass included, and never from a saved rule. Away mode,
   a denial or no answer refuses the start; nothing falls back to the
   requested model or to the session's model. A yes covers that model for
   the rest of the chat (its top-level chat, for a Task worker), and starts
   that need the same answer while the prompt is open wait for it instead
   of each raising a prompt of their own. An answered approval receipt in the
   existing ``pending_interactions`` table persists that exact chat/model
   pair across restarts; it is not a saved tool rule or a grant to other chats.
   Deleting the chat deletes its receipts. No schema migration is needed.

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

# (top-level chat id, model) pairs the owner approved at a prompt, and the
# prompts open right now, by the same key (see :func:`confirm`).
_APPROVED: set = set()
_ASKING: Dict[Tuple[str, str, Optional[str]], Any] = {}


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
        for index in range(start, len(text_tokens)):
            joined += text_tokens[index]
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

# Turn sources whose prompt is what the user typed into the chat: a message
# (``user``), one sent while a turn ran (``queue``), the first message of a
# chat started on a new branch (``worktree``) and a message sent through the
# HTTP API (``api``). Connector channels (other people can post there), peer
# messages, wakes, resumes and automations are not.
USER_SOURCES = ("user", "queue", "worktree", "api")
_SOURCES_SQL = ", ".join(f"'{source}'" for source in USER_SOURCES)


def _answer_text(cursor, session_id: str, request_id: Any, budget: List[int]) -> Optional[str]:
    """The user's answers to the agent's question ``request_id``, without the
    question itself (the agent wrote that), or None."""
    from app.core.user_question_handler import QUESTION_KIND, resolve_answer_envelope
    from app.db.base import parse_jsonb

    if not request_id:
        return None
    cursor.execute("SELECT payload, answer, length(CAST(payload AS TEXT)) + "
                   "length(CAST(answer AS TEXT)) AS scan_chars FROM pending_interactions "
                   "WHERE session_id=%s AND kind=%s AND payload ->> 'request_id'=%s "
                   "AND length(CAST(payload AS TEXT)) + length(CAST(answer AS TEXT)) <= %s LIMIT 1",
                   (session_id, QUESTION_KIND, request_id, budget[0]))
    for row in cursor.fetchall():
        budget[0] -= row["scan_chars"]
        payload = parse_jsonb(row.get("payload")) or {}
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            continue
        answers = resolve_answer_envelope(parse_jsonb(row.get("answer")))
        keys = [str(q.get("question") or "") for q in payload.get("questions") or [] if isinstance(q, dict)]
        return "\n".join(
            ", ".join(map(str, answers[key])) if isinstance(answers[key], list) else str(answers[key])
            for key in keys if key in answers)
    return None


def _typed(cursor, session_id: str, rows: Iterable[Dict[str, Any]], *,
           session: Optional[Dict[str, Any]] = None,
           answer_budget: Optional[List[int]] = None) -> List[str]:
    """The text the user typed in each turn row: a voice call's hand-off
    (written by the voice model) is left out, and an answer to the agent's
    question keeps only the answer."""
    from app.core.turn_manager import VOICE_DELEGATION
    from app.core.api_user_scope import session_api_user_id, turn_api_user_id
    from app.db import database
    from app.db.base import parse_jsonb

    session = session if session is not None else database.get_session(session_id)
    principal = session_api_user_id(session)
    texts = []
    for row in rows:
        prompt = row.get("prompt")
        payload = parse_jsonb(row.get("payload")) or {}
        payload = payload if isinstance(payload, dict) else {}
        if turn_api_user_id(payload, row.get("source"), session) != principal:
            continue
        if not isinstance(prompt, str) or payload.get(VOICE_DELEGATION):
            continue
        if "question_request_id" in payload:
            prompt = _answer_text(cursor, session_id, payload["question_request_id"],
                                  answer_budget if answer_budget is not None else [262144])
            if prompt is None:
                continue
        texts.append(prompt)
    return texts


def _user_rows(cursor, session_id: str, rows_left: int, chars_left: int) -> List[Dict[str, Any]]:
    """Newest delivered messages, bounded before their text leaves the database.

    The shared budget is 512 rows / 262144 characters (at most 1 MiB UTF-8),
    including payloads and question-answer envelopes, across the entire launch
    chain. Oversized history stops
    the window; a successful naming is saved separately and needs no rescan.
    """
    cursor.execute(
        "SELECT * FROM (SELECT recent.*, SUM(length(prompt) + "
        "length(COALESCE(CAST(payload AS TEXT), ''))) OVER (ORDER BY position DESC) AS scan_chars "
        "FROM (SELECT id, prompt, payload, source, position FROM turns WHERE session_id=%s "
        f"AND source IN ({_SOURCES_SQL}) AND state <> 'queued' "
        "ORDER BY position DESC LIMIT %s) recent) bounded "
        "WHERE scan_chars <= %s ORDER BY position", (session_id, rows_left, chars_left))
    return [dict(row) for row in cursor.fetchall()]


def chat_of(session_id: str) -> str:
    """Top-level Task chat, stopping at a missing or different-principal parent."""
    from app.core.api_user_scope import session_api_user_id
    from app.db import database
    from app.core.remote_task import report_owed

    current = session_id
    seen = {current}
    row = database.get_session(current)
    principal = session_api_user_id(row)
    for _ in range(_CHAIN_LIMIT):
        if not row or not row.get("is_worker") or report_owed(current):
            break
        parent = row.get("parent_session_id")
        parent_row = database.get_session(parent) if parent else None
        if not parent_row or parent in seen or session_api_user_id(parent_row) != principal:
            break
        current, row = parent, parent_row
        seen.add(current)
    return current


def _user_evidence(session_id: str) -> List[Dict[str, Any]]:
    """Same-principal user messages through real launch ancestry only."""
    try:
        from app.core.api_user_scope import session_api_user_id
        from app.core.session_orchestration import _start_turn
        from app.db import database
        from app.db.base import get_db

        current = chat_of(session_id)
        principal = session_api_user_id(database.get_session(session_id))
        seen = set()
        evidence = []
        rows_left, chars_left = 512, 262144
        with get_db() as conn:
            cursor = conn.cursor()
            for _ in range(_CHAIN_LIMIT + 1):
                row = database.get_session(current)
                if not row or current in seen or session_api_user_id(row) != principal:
                    break
                seen.add(current)
                rows = _user_rows(cursor, current, rows_left, chars_left)
                rows_left -= len(rows)
                chars_left -= max((r["scan_chars"] for r in rows), default=0)
                answer_budget = [chars_left]
                for turn in rows:
                    texts = _typed(cursor, current, [turn], session=row, answer_budget=answer_budget)
                    if texts:
                        evidence.append({"text": texts[0], "turn_id": turn["id"],
                                         "session_id": current, "api_user_id": principal})
                chars_left = answer_budget[0]
                if rows_left <= 0 or chars_left <= 0:
                    break
                # The sender on the durable StartSession launch is the immediate
                # parent. orchestration_root is a shared PEER budget, not consent.
                start = _start_turn(current) or {}
                payload = start.get("payload") or {}
                if payload.get("orchestration_remote_origin"):
                    break
                sender = payload.get("orchestration_sender")
                if not sender:
                    break
                current = chat_of(sender)
        return evidence
    except Exception:  # noqa: BLE001 — no evidence means the owner is asked
        logger.warning("Could not read the user's messages for %s", session_id, exc_info=True)
        return []


def _find_named(session_id: str, model: str) -> Optional[Dict[str, Any]]:
    evidence = _user_evidence(session_id)
    catalog = _catalog()
    match = next((row for row in evidence if named_by_user(model, [row["text"]], catalog)), None)
    if match:
        return match
    # First use after upgrade must not discard an old standing instruction.
    # Page by the indexed turn position rather than loading all history. A
    # five-second budget bounds the fallback, not the age of the instruction.
    # Saved ancestor naming also survives transcript retention and handoff.
    import time
    from app.core.api_user_scope import session_api_user_id
    from app.core.session_orchestration import _start_turn
    from app.db import database, interactions
    from app.db.base import get_db

    deadline = time.monotonic() + 5
    current = chat_of(session_id)
    principal = session_api_user_id(database.get_session(session_id))
    seen = set()
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            for _ in range(_CHAIN_LIMIT + 1):
                session = database.get_session(current)
                if (not session or current in seen
                        or session_api_user_id(session) != principal):
                    break
                seen.add(current)
                if _has_named(current, model):
                    payload = interactions.get_interaction(_named_id(current, model))["payload"]
                    return {k: payload[k] for k in ("turn_id", "session_id", "api_user_id")}
                before = float("inf")
                while time.monotonic() < deadline:
                    cursor.execute(
                        "SELECT id, source, position, "
                        "CASE WHEN length(prompt) + length(CAST(payload AS TEXT)) <= 65536 "
                        "THEN prompt END AS prompt, "
                        "CASE WHEN length(prompt) + length(CAST(payload AS TEXT)) <= 65536 "
                        "THEN payload END AS payload FROM turns WHERE session_id=%s "
                        f"AND source IN ({_SOURCES_SQL}) AND state <> 'queued' "
                        "AND position < %s ORDER BY position DESC LIMIT 4",
                        (current, before))
                    rows = [dict(row) for row in cursor.fetchall()]
                    if not rows:
                        break
                    for turn in rows:
                        if time.monotonic() >= deadline:
                            return None
                        texts = _typed(cursor, current, [turn], session=session)
                        if named_by_user(model, texts, catalog):
                            return {"turn_id": turn["id"], "session_id": current,
                                    "api_user_id": principal}
                    before = rows[-1]["position"]
                if time.monotonic() >= deadline:
                    break
                payload = (_start_turn(current) or {}).get("payload") or {}
                if payload.get("orchestration_remote_origin") or not payload.get("orchestration_sender"):
                    break
                current = chat_of(payload["orchestration_sender"])
    except Exception:  # noqa: BLE001 — a failed search never grants consent
        logger.warning("Could not search old model naming for %s", session_id, exc_info=True)
    return None


def user_texts(session_id: str) -> List[str]:
    """Same-principal typed messages inside the bounded launch-chain window."""
    return [row["text"] for row in _user_evidence(session_id)]


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

# Durable receipts are already-answered interactions, never pending prompts.
# This kind is distinct from a permission rule and is written only after the
# real ModelSwitch permission path returns Allow.
_APPROVAL_KIND = "model_switch_approval"
_NAMED_KIND = "model_switch_named"
RECEIPT_KINDS = (_APPROVAL_KIND, _NAMED_KIND)


def _named_id(chat_id: str, model: str) -> str:
    import json

    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([_NAMED_KIND, chat_id, model])))


def _has_named(chat_id: str, model: str) -> bool:
    from app.core.api_user_scope import viewer_api_user_id
    from app.db import interactions

    try:
        row = interactions.get_interaction(_named_id(chat_id, model)) or {}
        payload = row.get("payload") or {}
        return (row.get("session_id") == chat_id and row.get("kind") == _NAMED_KIND
                and row.get("state") == "answered" and row.get("answer") == {"decision": "allow"}
                and isinstance(payload, dict) and payload.get("model") == model
                and payload.get("source") == "own_message"
                and payload.get("api_user_id") == viewer_api_user_id(chat_id)
                and bool(payload.get("turn_id")) and bool(payload.get("session_id")))
    except Exception:  # noqa: BLE001 — missing evidence never grants a switch
        logger.warning("Could not read model naming for %s", chat_id, exc_info=True)
        return False


def _remember_named(chat_id: str, model: str, evidence: Dict[str, Any]) -> bool:
    import json
    from app.db.base import get_db
    from app.db.session_events import notify_session_write

    payload = {k: evidence[k] for k in ("turn_id", "session_id", "api_user_id")}
    payload.update(model=model, source="own_message")
    try:
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO pending_interactions "
                "(id, session_id, kind, payload, state, answer, answered_at) "
                "VALUES (%s, %s, %s, %s, 'answered', %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, "
                "state=EXCLUDED.state, answer=EXCLUDED.answer, answered_at=EXCLUDED.answered_at",
                (_named_id(chat_id, model), chat_id, _NAMED_KIND,
                 json.dumps(payload), json.dumps({"decision": "allow"})))
        notify_session_write(chat_id, "pending_interactions")
        return True
    except Exception:  # noqa: BLE001 — this start has evidence even if caching fails
        logger.exception("Could not save model naming for %s", chat_id)
        return False


def _approval_id(chat_id: str, model: str) -> str:
    import json

    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([_APPROVAL_KIND, chat_id, model])))


def _has_approval(chat_id: str, model: str) -> bool:
    from app.core.api_user_scope import viewer_api_user_id
    from app.db import interactions

    try:
        row = interactions.get_interaction(_approval_id(chat_id, model)) or {}
        return (row.get("session_id") == chat_id and row.get("kind") == _APPROVAL_KIND
                and row.get("state") == "answered"
                and row.get("payload") == {"model": model, "api_user_id": viewer_api_user_id(chat_id)}
                and row.get("answer") == {"decision": "allow"})
    except Exception:  # noqa: BLE001 — missing evidence never grants a switch
        logger.warning("Could not read model approval for %s", chat_id, exc_info=True)
        return False


def _remember_approval(chat_id: str, model: str) -> bool:
    import json
    from app.core.api_user_scope import viewer_api_user_id
    from app.db.base import get_db
    from app.db.session_events import notify_session_write

    try:
        # Insert the receipt atomically, already answered: no transient pending
        # card, no TTL, and no lost updates when different models are approved.
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO pending_interactions "
                "(id, session_id, kind, payload, state, answer, answered_at) "
                "VALUES (%s, %s, %s, %s, 'answered', %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, "
                "state=EXCLUDED.state, answer=EXCLUDED.answer, answered_at=EXCLUDED.answered_at",
                (_approval_id(chat_id, model), chat_id, _APPROVAL_KIND,
                 json.dumps({"model": model, "api_user_id": viewer_api_user_id(chat_id)}),
                 json.dumps({"decision": "allow"})))
        notify_session_write(chat_id, "pending_interactions")
        return True
    except Exception:  # noqa: BLE001 — do not claim a durable approval was saved
        logger.exception("Could not save model approval for %s", chat_id)
        return False


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

    from app.core.api_user_scope import viewer_api_user_id

    key = (await asyncio.to_thread(chat_of, session_id), model)
    cache_key = (*key, await asyncio.to_thread(viewer_api_user_id, key[0]))
    if cache_key in _APPROVED:
        return APPROVED
    if await asyncio.to_thread(_has_approval, *key):
        _APPROVED.add(cache_key)
        return APPROVED
    if await asyncio.to_thread(_has_named, *key):
        return NAMED
    evidence = await asyncio.to_thread(_find_named, session_id, model)
    if evidence:
        await asyncio.to_thread(_remember_named, *key, evidence)
        return NAMED
    while cache_key not in _APPROVED:
        pending = _ASKING.get(cache_key)
        if pending is None:
            break
        # The same switch is already being asked: share that one answer.
        outcome = await asyncio.shield(pending)
        if outcome is None:
            continue  # that prompt was abandoned before an answer
        if outcome != APPROVED:
            raise PermissionError(_refusal(
                model, baseline,
                f"no owner message named it and no prior chat approval was found; {outcome}"))
    if cache_key in _APPROVED:
        return APPROVED
    future = asyncio.get_running_loop().create_future()
    _ASKING[cache_key] = future
    outcome = None
    try:
        outcome = await _ask_owner(session_id, profile_id, model=model, baseline=baseline, action=action)
        if outcome == APPROVED:
            if await asyncio.to_thread(_remember_approval, *key):
                _APPROVED.add(cache_key)
            else:
                outcome = "the approval could not be saved; nothing was started."
    finally:
        _ASKING.pop(cache_key, None)
        if not future.done():
            future.set_result(outcome)
    if outcome != APPROVED:
        raise PermissionError(_refusal(
            model, baseline,
            f"no owner message named it and no prior chat approval was found; {outcome}"))
    return APPROVED


async def _ask_owner(session_id: str, profile_id: Optional[str], *, model: str,
                     baseline: Optional[str], action: str) -> str:
    """:data:`APPROVED`, else why not ("the owner declined the switch.")."""
    import asyncio

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
        "decision_reason": ("The agent asked for a model you did not name in this chat. "
                            "A yes covers this model for this chat, including after app restarts."),
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
        return "the owner is away, so no approval prompt was shown and the switch was refused."
    if "timed out" in message:
        return "the owner did not answer the prompt."
    if result is None:
        return "the owner could not be asked."
    return "the owner declined the switch."


# ---------------------------------------------------------------------------
# Run settings
# ---------------------------------------------------------------------------

def settings_for_model(model: str, provider_id: Optional[str], *, effort: Any = None,
                       context_window: Any = None, fast: Any = None) -> Dict[str, Any]:
    """The inherited run settings that still apply to ``model``.

    An effort, context window or fast mode the new model does not support is
    dropped (None), so it starts on that model's own default instead of a
    setting its provider would reject. The context window is the user's
    standard-or-1M choice, so it is first fitted to the new model the way a
    Task worker's is (a GPT 272K "standard" becomes Claude's 200K).
    """
    from app.core.model_effort import normalize_effort
    from app.core.model_registry import context_window_choices
    from app.core.provider_agent import worker_context_window
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.core.query_options import decode_provider_model
    from app.db.model_providers import get_model_provider

    decoded = decode_provider_model(model)
    bare = decoded[1] if decoded else model
    provider = get_model_provider(provider_id) if provider_id else None
    route = _decide_provider_kind(SessionOptions(
        model=bare, extra={"provider_id": provider_id} if provider_id else {}))
    window = worker_context_window(context_window, provider_id, bare)
    return {
        "effort": normalize_effort(effort, bare, provider, route=route) if effort else None,
        "context_window": (window if window
                           and window in context_window_choices(bare, provider) else None),
        "fast": fast if not fast or route in ("delegated", "openai_responses") else None,
    }


# GPT-5 and versioned GPT-6 models, including the named ChatGPT variants: the
# same rule as the composer's toggle (frontend ``isFastModeCapable``).
_FAST_OPENAI_MODEL = re.compile(r"^(?:gpt-5|gpt-6(?:\.\d+)*(?:-(?:astra|sol|luna|terra))?$)", re.I)


def fast_mode_supported(model: Optional[str], provider_id: Optional[str]) -> bool:
    """Whether fast mode applies to ``model`` on ``provider_id``.

    The rule the composer uses to offer the toggle: an Opus model on the
    Claude CLI (its ``fastMode`` setting), or a GPT-5/GPT-6 model on the
    OpenAI Responses lane (``service_tier: priority``). Every other model, and
    an unknown one, is False. Used when fast mode is inherited rather than
    picked, so a subagent on such a model keeps its own setting.
    """
    from app.core.model_registry import get_model_family
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.core.query_options import decode_provider_model

    model = (model or "").strip()
    if not model:
        return False
    decoded = decode_provider_model(model)
    if decoded:
        provider_id, model = decoded
    try:
        route = _decide_provider_kind(SessionOptions(
            model=model, extra={"provider_id": provider_id} if provider_id else {}))
    except Exception:  # noqa: BLE001 — an unresolvable route cannot run fast
        logger.debug("fast mode: could not resolve the route for %s", model, exc_info=True)
        return False
    if route == "openai_responses":
        return bool(_FAST_OPENAI_MODEL.match(model))
    if route == "delegated":
        return get_model_family(model) == "opus"
    return False
