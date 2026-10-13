"""The owner's one "subagent default" model.

Every agent the lead dispatches runs on, in order (#1827, #1830):

1. a model named for this task, used only when the user asked for it (see
   ``app.core.model_override``);
2. the agent's pinned model for the caller's provider (``provider_models``);
3. this global default;
4. the owner's answer in their chat (``ask_in_chat``), saved as this default;
   with no owner to ask, or no answer, the start is refused and the owner is
   notified once. An unpinned agent never falls back to the session's model.

Stored as ONE shared system setting (``subagent_default``, JSON
``{"model": ...}``), like the text-generation model: the network's
``system_settings`` collection syncs it to every worker of the account, and
the dispatch path reads it without an account-hub lookup. Settings written
before #1830 also carry an ``allowed`` list; it is ignored.

Values use the app's model id format: a Claude registry alias (``sonnet``) or
a ``prov:<providerId>:<modelId>`` composite. Writes are validated against the
models this install can run (the Claude registry, or an enabled provider's
curated list); reads tolerate anything, so a value that stopped being
available is reported, never crashes a dispatch.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = "subagent_default"

NOTIFICATION_CATEGORY = "subagent_default_needed"

# Where the owner chooses it, named in every refusal so neither the lead agent
# nor the owner has to guess.
SETTINGS_HINT = "The owner sets it on the Agents page (Subagent default)."


@dataclass(frozen=True)
class SubagentDefault:
    model: Optional[str] = None


def read() -> SubagentDefault:
    """The stored setting, shape-checked only. Never raises."""
    try:
        from app.db import database

        raw = database.get_system_setting(SETTING_KEY)
    except Exception:  # noqa: BLE001 — an unreadable setting reads as unset
        logger.debug("Could not read the subagent default", exc_info=True)
        return SubagentDefault()
    if not raw:
        return SubagentDefault()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return SubagentDefault()
    if not isinstance(data, dict):
        return SubagentDefault()
    model = data.get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else None
    return SubagentDefault(model=model)


def normalize_model(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(app-format id, None)`` for a model this install can run, else ``(None, reason)``.

    A Claude id normalizes onto its registry alias (``claude-opus-4-6`` ->
    ``opus``). A composite must name an enabled provider whose curated list
    holds the model, the same check an agent's pin passes at roster time.
    """
    if not isinstance(value, str) or not value.strip():
        return None, "A model id is required."
    text = value.strip()
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(text)
    if decoded is not None:
        from app.core.provider_agent import validate_provider_model

        validation = validate_provider_model(decoded[0], decoded[1], require_servable=False)
        if not validation.ok:
            return None, validation.reason or f"Model '{text}' is not available."
        return f"prov:{decoded[0]}:{decoded[1]}", None
    if text.startswith("prov:"):
        return None, f"'{text}' is not a valid prov:<providerId>:<modelId> id."
    if is_native_claude_model(text):
        from app.core.model_registry import model_values, normalize_model as _alias

        alias = _alias(text)
        if alias in model_values():
            return alias, None
        return None, (
            f"'{text}' is not a Claude model this app offers. "
            f"Choose one of: {', '.join(model_values())}."
        )
    return None, (
        f"'{text}' is not a Claude model. A provider's model is written "
        "prov:<providerId>:<modelId>."
    )


def write(model: Optional[str]) -> SubagentDefault:
    """Validate and save the setting. Raises ``ValueError`` on a bad id."""
    from app.db import database

    clean_model: Optional[str] = None
    if model is not None and (not isinstance(model, str) or model.strip()):
        clean_model, error = normalize_model(model)
        if error:
            raise ValueError(f"model: {error}")
    database.set_system_setting(SETTING_KEY, json.dumps({"model": clean_model}))
    if clean_model:
        # A later "no default" state (cleared again) may ask once more.
        reset_asked()
    return SubagentDefault(model=clean_model)


def usable(value: Optional[str]) -> Optional[str]:
    """``value`` normalized when it can still run here, else ``None``."""
    if not value:
        return None
    normalized, error = normalize_model(value)
    return None if error else normalized


def effective() -> SubagentDefault:
    """The stored setting, the model dropped when it can no longer run."""
    return SubagentDefault(model=usable(read().model))


def payload() -> Dict[str, Any]:
    """The GET body: the stored choice."""
    return {"model": read().model}


# ---------------------------------------------------------------------------
# Asking the owner once
# ---------------------------------------------------------------------------

_asked = False
_PENDING: set = set()


def reset_asked() -> None:
    global _asked
    _asked = False


def ask_owner_once(session_id: Optional[str] = None) -> bool:
    """Notify the owner, once per process, that subagents need a default.

    The Task refusal tells the lead agent to pass the request on; this bell
    item reaches the owner on every device even if the lead does not. Returns
    True when a notification was scheduled. Never raises.
    """
    global _asked
    if _asked:
        return False
    _asked = True
    try:
        import asyncio

        from app.services.notification_service import emit_notification

        coro = emit_notification(
            "admin",
            severity="warning",
            title="Choose a model for subagents",
            message=(
                "An agent tried to start a subagent, but no subagent default "
                "model is set and that agent has no model of its own. Choose "
                "the subagent default on the Agents page; every agent without "
                "its own model will use it."
            ),
            category=NOTIFICATION_CATEGORY,
            session_id=session_id,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Hold a reference until it finishes: the loop keeps only a weak one.
            task = loop.create_task(coro)
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
        else:
            asyncio.run(coro)
        return True
    except Exception:  # noqa: BLE001 — the refusal still tells the lead agent
        logger.warning("Could not notify the owner about the subagent default", exc_info=True)
        return False


def missing_default_error(agent_label: str, why: Optional[str] = None) -> str:
    """The refusal a lead agent gets for an unpinned agent with no default.

    ``why`` says what happened when the owner was asked in the chat (or why
    they could not be), e.g. "the owner dismissed the question."
    """
    told = (
        f"They were asked in the chat, but {why} " if why
        else "They have been notified. "
    )
    text = (
        f"Subagent '{agent_label}' has no model: it is not pinned to one and the "
        "owner has not chosen a subagent default yet, and a subagent never runs "
        "on this session's model by fallback. Nothing was dispatched. Tell the "
        "owner to choose a subagent default. "
        + told
        + SETTINGS_HINT
        + " Do not retry this agent until they have."
    )
    return text + (
        " If the user named a model for this task, pass it as model; that is "
        "checked and used for this task only."
    )


# ---------------------------------------------------------------------------
# Asking the owner in the chat (#1827)
# ---------------------------------------------------------------------------
#
# At the first delegation with no default, the owner's chat shows a question
# card with a model picker. The answer is saved as the default and the
# delegation carries on. It rides the ordinary blocking question flow
# (``pending_interactions``), so it reaches every owner device, survives a
# reload and is answered only from the owner's UI. The question has NO
# options on purpose: Away mode answers a question with its recommended or
# first option, and a model must never be chosen for the owner.

QUESTION_TEXT = "Which model should subagents use?"
QUESTION_HEADER = "Subagent model"
# The card renders a model picker instead of options for this question.
PICKER_MODEL = "model"
# Shorter than the hour a question normally waits: the dispatch holds a tool
# call (and, from a worker, that worker's turn) open while it waits.
ASK_TTL_SECONDS = 900
# After a dismissal, Task calls from the same chat in this window are refused
# without asking again, so several delegations in one go see one card.
DISMISS_QUIET_SECONDS = 15.0

NO_OWNER = (
    "no owner is present to choose one (this chat runs as an automation, "
    "through the API, as a background worker or in Away mode)."
)


@dataclass(frozen=True)
class AskOutcome:
    """The model the owner chose, else ``reason`` completes "They were asked, but ..."."""

    model: Optional[str] = None
    reason: str = ""
    # True when nobody saw a card, so the bell notice should still go out.
    notify: bool = False


_ASKING: Optional["asyncio.Future[AskOutcome]"] = None
_DISMISSED_AT: Dict[str, float] = {}


def owner_chat(session_id: Optional[str]) -> Optional[str]:
    """The top-level chat where the owner can answer in person, else None. Blocking.

    None for API-key chats, automation runs, hidden or voice sessions, and a
    worker whose launch chain does not reach an owner chat.
    """
    if not session_id:
        return None
    try:
        from app.core.model_override import chat_of
        from app.db import database

        root = chat_of(session_id)
        row = database.get_session(root)
    except Exception:  # noqa: BLE001 — unknown means nobody can be asked
        logger.debug("Owner chat lookup failed for %s", session_id, exc_info=True)
        return None
    if not row:
        return None
    if (row.get("api_user_id") or row.get("automation_id") or row.get("is_worker")
            or row.get("hidden") or row.get("voice_config")):
        return None
    return root


async def ask_in_chat(session_id: Optional[str]) -> AskOutcome:
    """Ask the owner in their chat which model subagents use, and save it.

    One question at a time for the whole app: concurrent Task calls share its
    answer. Never raises except for cancellation of the caller.
    """
    import asyncio
    import time

    global _ASKING
    current = effective().model
    if current:
        return AskOutcome(model=current)
    root = await asyncio.to_thread(owner_chat, session_id)
    if root is None:
        return AskOutcome(reason=NO_OWNER, notify=True)
    from app.core.away_mode import answers_questions_async

    if await answers_questions_async(root):
        return AskOutcome(reason=NO_OWNER, notify=True)
    pending = _ASKING
    if pending is not None and not pending.done():
        # The question is already on screen: share that one answer.
        return await asyncio.shield(pending)
    dismissed_at = _DISMISSED_AT.get(root)
    if dismissed_at is not None and time.monotonic() - dismissed_at < DISMISS_QUIET_SECONDS:
        return AskOutcome(reason="the owner dismissed the question.")
    future: "asyncio.Future[AskOutcome]" = asyncio.get_running_loop().create_future()
    _ASKING = future
    outcome = AskOutcome(reason="the question could not be shown.", notify=True)
    try:
        outcome = await _ask(root)
    finally:
        if _ASKING is future:
            _ASKING = None
        if not future.done():
            future.set_result(outcome)
    if outcome.model is None and outcome.reason.startswith("the owner dismissed"):
        _DISMISSED_AT[root] = time.monotonic()
    else:
        _DISMISSED_AT.pop(root, None)
    return outcome


async def _ask(root: str) -> AskOutcome:
    import asyncio
    import uuid

    from app.core.sync_engine import sync_engine
    from app.core.user_question_handler import (
        DISMISSED_KEY,
        publish_session_attention,
        user_question_handler,
    )

    request_id = f"subagent-default-{uuid.uuid4().hex[:12]}"
    questions = [{
        "question": QUESTION_TEXT,
        "header": QUESTION_HEADER,
        "options": [],
        "multiSelect": False,
        "picker": PICKER_MODEL,
    }]

    async def _to_owner(payload: Dict[str, Any]) -> None:
        # No socket owns this prompt: every owner device gets it (and its
        # timeout), and the chat's page dedupes by request id.
        await sync_engine.broadcast_user_question("admin", payload)

    async def _attention(_payload: Dict[str, Any]) -> None:
        await publish_session_attention(root)

    try:
        result = await user_question_handler._request_answers_v2(
            request_id=request_id,
            tool_use_id=request_id,
            session_id=root,
            questions=questions,
            broadcast_func=_to_owner,
            fanout_func=_attention,
            ttl_seconds=ASK_TTL_SECONDS,
        )
    except asyncio.CancelledError:
        await _withdraw(root, request_id)
        raise
    except Exception:  # noqa: BLE001 — a failed card refuses like before
        logger.exception("Could not ask for the subagent default in %s", root)
        await _withdraw(root, request_id)
        return AskOutcome(reason="the question could not be shown.", notify=True)
    if not isinstance(result, dict) or result.get(DISMISSED_KEY):
        return AskOutcome(reason="the owner dismissed the question.")
    error = result.get("error")
    if error:
        if "timed out" in str(error):
            return AskOutcome(reason="the owner did not answer in time.")
        return AskOutcome(reason="the question was withdrawn before an answer.")
    answer = result.get(QUESTION_TEXT)
    if isinstance(answer, list):
        answer = answer[0] if answer else None
    try:
        saved = await asyncio.to_thread(write, answer if isinstance(answer, str) else None)
    except ValueError as exc:
        return AskOutcome(reason=f"the answer was not a model this app can run ({exc}).")
    except Exception:  # noqa: BLE001
        logger.exception("Could not save the subagent default chosen in %s", root)
        return AskOutcome(reason="the choice could not be saved.")
    if not saved.model:
        return AskOutcome(reason="no model was chosen.")
    logger.info("Owner chose subagent default %s in chat %s", saved.model, root)
    return AskOutcome(model=saved.model)


async def _withdraw(root: str, request_id: str) -> None:
    """Retire a question nobody waits on any more, and close its card. Never raises."""
    import asyncio

    try:
        from app.core.sync_engine import sync_engine
        from app.core.user_question_handler import user_question_handler

        rows = await asyncio.to_thread(user_question_handler._v2_pending_questions, root)
        for row in rows:
            if (row.get("payload") or {}).get("request_id") == request_id:
                await user_question_handler._retire_unanswered(root, row["id"], request_id)
        await sync_engine.broadcast_user_question("admin", {
            "type": "user_question_timeout", "session_id": root, "request_id": request_id,
        })
    except Exception:  # noqa: BLE001
        logger.debug("Could not withdraw subagent default question %s", request_id, exc_info=True)
