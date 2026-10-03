"""Spoken approvals on a live call attached to a conversation.

When work in the conversation asks for permission during a call, the voice
asks the caller in one plain sentence and the caller answers out loud. The
decision is made HERE, by app code, from the transcript of the caller's own
audio turn after the question was put to them:

* a firm yes approves, a firm no declines;
* anything hedged, conditional, delegating ("you decide", "I guess", "maybe",
  "if you think so") or off-topic is unclear: the voice asks once more, and a
  second unclear answer declines;
* the realtime model's judgment, text it read out of tool output and typed
  text never decide anything, and one yes answers exactly one named request.

The answer goes through ``permission_handler.respond`` (the same call a tap on
the card makes) after the tap's authority checks are re-run for the call's
caller. Requests the app keeps on screen (private access, anything carrying a
secret) are never answered by voice; the voice says they need the screen.

:func:`classify_spoken_answer` is the one decision function; it uses the shared
``app.core.assistant_confirm.classify``, the same check typed answers get. When
a request carries the assistant's own question (``assistant_confirm``), the
outcome is also recorded the way a typed answer's is, so the conversation and
the text model see it.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional

from app.core.assistant_confirm import MAX_UNCLEAR, UNCLEAR, YES

logger = logging.getLogger(__name__)


# How many unclear answers end in a decline: the first asks again.
MAX_UNCLEAR_ANSWERS = MAX_UNCLEAR
SUMMARY_MAX_CHARS = 160


_NOISE_WORDS = frozenset({"uh", "um", "umm", "hm", "hmm", "mm", "mmm", "mhm", "ah", "oh", "er", "erm", "huh"})


def is_noise(text: str) -> bool:
    """No words at all, or only breath and filler sounds: not an answer attempt."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("’", "'").replace("'", ""))
    return all(word in _NOISE_WORDS for word in words)


def classify_spoken_answer(text: str) -> str:
    """``yes``, ``no`` or ``unclear`` for one spoken answer.

    The shared check for answers given in the assistant's conversation, so a
    spoken and a typed answer are judged by the same rule.
    """
    from app.core import assistant_confirm
    return assistant_confirm.classify(text)


# Tools whose approval belongs to the private-access card, and argument names
# that mean a secret travels with the request. Neither is answered by voice.
_SCREEN_TOOLS = frozenset({
    "RequestChatAccess", "ChatResources", "ChatRunCommand", "ChatWriteFile",
    "ChatReadFile", "ChatListFiles", "ChatHttpRequest",
})
_SECRET_ARG = re.compile(
    r"secret|token|password|passwd|passphrase|api[_-]?key|credential|private[_-]?key|cookie|bearer",
    re.IGNORECASE,
)


def _has_secret(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        return any((isinstance(k, str) and _SECRET_ARG.search(k) and v not in (None, "", [], {}))
                   or _has_secret(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_secret(v, depth + 1) for v in value[:40])
    return False


def needs_screen(request: Dict[str, Any]) -> Optional[str]:
    """Why this request must be answered on screen, or None when voice may."""
    name = str(request.get("tool_name") or "")
    short = name.rsplit("__", 1)[-1]
    if short in _SCREEN_TOOLS or "chat-access" in name or "chat_access" in name:
        return "Private access is approved in its card on screen."
    if _has_secret(request.get("tool_input")):
        return "It carries a secret, which is only handled on screen."
    return None


def request_summary(request: Dict[str, Any]) -> str:
    """One short, speakable line naming what the request would do."""
    confirm = request.get("assistant_confirm")
    if isinstance(confirm, dict) and isinstance(confirm.get("question"), str) and confirm["question"].strip():
        # The assistant's own plain question for this request, when it has one.
        return " ".join(confirm["question"].split())[:SUMMARY_MAX_CHARS]
    for key in ("display_name", "title", "description", "tool_name"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS - 1] + "…"
    return "A tool needs permission to run"


@dataclass
class Ask:
    request_id: str
    summary: str
    delivered: bool = False
    marker: Any = None
    unclear: int = 0
    # The assistant's own question for this request (assistant_confirm), if any.
    confirm: Optional[Dict[str, Any]] = None


@dataclass
class Outcome:
    request_id: str
    verdict: str  # yes / no / unclear
    text: str
    final: bool


class SpokenApprovals:
    """Per-call desk: which request is being asked, and what the caller said.

    ``marker`` returns the transport's current position in the caller's audio
    (GPT Live: transcript sequence; Realtime: audio item count). Only speech
    after the position recorded when the question reached the voice counts as
    the answer.
    """

    def __init__(self, session_id: str, *, api_user_id: Optional[str] = None,
                 marker: Optional[Callable[[], Any]] = None) -> None:
        self.session_id = session_id
        self.api_user_id = api_user_id
        self._marker = marker or (lambda: None)
        self.current: Optional[Ask] = None
        self.queue: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.handled: set = set()

    @property
    def awaiting(self) -> bool:
        """A question has been put to the caller and is waiting for an answer."""
        return self.current is not None and self.current.delivered

    def offer(self, request: Dict[str, Any]) -> Optional[str]:
        """Take a new pending request; return the note to put to the voice."""
        request_id = str(request.get("request_id") or "")
        if (not request_id or request_id in self.handled or request_id in self.queue
                or (self.current and self.current.request_id == request_id)):
            return None
        summary = request_summary(request)
        reason = needs_screen(request)
        if reason:
            self.handled.add(request_id)
            return (f"A decision is waiting on screen and cannot be answered by voice. {reason} "
                    f"Tell the user in one sentence to answer it on screen: {summary}")
        if self.current is not None:
            self.queue[request_id] = request
            return None
        confirm = request.get("assistant_confirm")
        self.current = Ask(request_id, summary, confirm=confirm if isinstance(confirm, dict) else None)
        return self.ask_text(self.current)

    @staticmethod
    def ask_text(ask: Ask) -> str:
        return ("Approval needed. Ask the user in one plain sentence whether to allow this, then "
                f"wait for their yes or no; the app takes the answer from their voice: {ask.summary}")

    def delivered(self, request_id: str) -> None:
        """The question reached the voice; speech from now on is the answer."""
        if self.current and self.current.request_id == request_id and not self.current.delivered:
            self.current.delivered = True
            self.current.marker = self._marker()

    def next_offer(self) -> Optional[tuple]:
        """Promote the next queued request: ``(request_id, note)`` or None."""
        while self.current is None and self.queue:
            _, request = self.queue.popitem(last=False)
            text = self.offer(request)
            if text:
                return str(request.get("request_id")), text
        return None

    def forget(self, request_id: str) -> Optional[str]:
        """The request was answered elsewhere or expired. Returns a quiet note."""
        if not request_id:
            return None
        self.queue.pop(request_id, None)
        if self.current and self.current.request_id == request_id:
            ask, self.current = self.current, None
            self.handled.add(request_id)
            if ask.delivered:
                return f"The approval for {ask.summary} was settled on screen. Do not ask about it again."
        self.handled.add(request_id)
        return None

    async def hear(self, transcript: str) -> Optional[Outcome]:
        """Decide the waiting request from the caller's own spoken answer."""
        ask = self.current
        if ask is None or not ask.delivered or is_noise(transcript):
            return None
        verdict = classify_spoken_answer(transcript)
        if verdict == UNCLEAR:
            ask.unclear += 1
            if ask.unclear < MAX_UNCLEAR_ANSWERS:
                ask.marker = self._marker()
                return Outcome(ask.request_id, UNCLEAR, (
                    "That answer was not a firm yes or no, so nothing was decided. Say you need a "
                    f"clear yes or no and ask once more, in one short sentence: {ask.summary}"), False)
        self.current = None
        self.handled.add(ask.request_id)
        decision: Literal["allow", "deny"] = "allow" if verdict == YES else "deny"
        text = await self._answer(ask, decision, second_unclear=verdict == UNCLEAR)
        return Outcome(ask.request_id, verdict, text, True)

    async def _answer(self, ask: Ask, decision: Literal["allow", "deny"], *, second_unclear: bool) -> str:
        from app.core.permission_handler import permission_handler
        from app.db.async_database import run_sync

        refusal = await run_sync(self._refusal, ask.request_id)
        if refusal:
            logger.info("Voice approval for %s refused (%s)", self.session_id, refusal)
            return (f"The spoken answer could not be recorded: {refusal} Tell the user to answer it "
                    f"on screen: {ask.summary}")
        result = await permission_handler.respond(
            request_id=ask.request_id, session_id=self.session_id, decision=decision,
            remember=None, broadcast_func=None,
        )
        if not isinstance(result, dict) or result.get("error"):
            return f"The approval for {ask.summary} was already settled or has expired. Nothing more to do."
        logger.info("Voice approval %s for %s answered %s by the caller's voice",
                    ask.request_id, self.session_id, decision)
        await self._record_confirm(ask, decision)
        if second_unclear:
            return f"No firm yes was heard, so the request was declined: {ask.summary}. Tell the user briefly."
        verb = "approved" if decision == "allow" else "declined"
        return f"The user {verb} it by voice and the app recorded it: {ask.summary}. Confirm in a few words."

    async def _record_confirm(self, ask: Ask, decision: str) -> None:
        """Record an answer to the assistant's own question as a typed one is.

        The conversation shows the outcome row and the text model's next turn
        is told what the app decided. Best effort: the answer already stands.
        """
        if ask.confirm is None:
            return
        try:
            from app.core import assistant_confirm
            request = {"request_id": ask.request_id, "question": ask.confirm.get("question") or ask.summary,
                       "chat_session_id": ask.confirm.get("chat_session_id")}
            outcome = "approved" if decision == "allow" else "declined"
            assistant_confirm._remember(self.session_id, request, outcome)
            await assistant_confirm._record(self.session_id, request, outcome)
        except Exception:  # noqa: BLE001
            logger.warning("Voice approval: could not record the outcome for %s", self.session_id, exc_info=True)

    def _refusal(self, request_id: str) -> Optional[str]:
        """The tap's authority checks, re-run for this call. Synchronous."""
        from fastapi import HTTPException

        from app.api.sessions_helpers import check_session_access_for_api_user
        from app.core.permission_handler import permission_handler
        from app.db import database

        row = database.get_session(self.session_id)
        if not row:
            return "the conversation no longer exists."
        if self.api_user_id:
            user = database.get_api_user(self.api_user_id)
            if not user or not user.get("is_active", True):
                return "this call's API key no longer has access."
            try:
                check_session_access_for_api_user(dict(user), dict(row))
            except HTTPException:
                return "this call's API key may not answer requests in this conversation."
        try:
            from app.network import control
            if control.owner_of(dict(row)):
                return "this conversation runs on another device."
        except Exception:  # noqa: BLE001 - no network layer means it is ours
            logger.debug("Voice approval: owner lookup failed", exc_info=True)
        request = next((r for r in permission_handler.get_pending_requests(self.session_id)
                        if r.get("request_id") == request_id), None)
        if request is not None:
            reason = needs_screen(request)
            if reason:
                return reason
        return None
