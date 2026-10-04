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
  text never decide anything, and one yes answers exactly one named request,
  plus a retry of that same tool on the same target by the same asker within
  :data:`RETRY_WINDOW_SECONDS` on this call. It never carries to a different
  action.

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
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional

from app.core.assistant_confirm import MAX_UNCLEAR, UNCLEAR, YES

logger = logging.getLogger(__name__)


# How many unclear answers end in a decline: the first asks again.
MAX_UNCLEAR_ANSWERS = MAX_UNCLEAR
SUMMARY_MAX_CHARS = 160
# A spoken yes also covers a retry of the same tool on the same target, from
# the same asker, raised within this many seconds of the last approval on this
# call (#1797: every retry of a failed image edit needed a new yes).
RETRY_WINDOW_SECONDS = 300.0

# Arguments that name what a tool acts on, most specific first. A request with
# none of them has no target, so a yes is never carried to it.
_TARGET_ARGS = ("file_path", "notebook_path", "image_path", "video_path", "audio_path",
                "reference_images", "path", "url", "command")


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


def _job_label(request: Dict[str, Any]) -> Optional[str]:
    """The background job (worker) that raised this request, if any."""
    tag = request.get("provider_agent")
    label = tag.get("label") if isinstance(tag, dict) else None
    return " ".join(label.split())[:80] if isinstance(label, str) and label.strip() else None


def request_summary(request: Dict[str, Any]) -> str:
    """One short, speakable line naming what the request would do."""
    confirm = request.get("assistant_confirm")
    if isinstance(confirm, dict) and isinstance(confirm.get("question"), str) and confirm["question"].strip():
        # The assistant's own plain question for this request, when it has one.
        return " ".join(confirm["question"].split())[:SUMMARY_MAX_CHARS]
    # A background job's request says whose it is (#1797).
    job = _job_label(request)
    prefix = f"For the background job “{job}”: " if job else ""
    for key in ("display_name", "title", "description", "tool_name"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            text = prefix + " ".join(value.split())
            return text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS - 1] + "…"
    return prefix + "A tool needs permission to run"


def retry_key(request: Dict[str, Any]) -> Optional[tuple]:
    """``(asker, tool, target)`` identifying the same action on the same target.

    The asker is the worker or started chat that raised it (the home when
    neither), so a yes for the assistant's request never covers a background
    job's, and the reverse. None when the request names no target.
    """
    tool = str(request.get("tool_name") or "")
    data = request.get("tool_input")
    if not tool or not isinstance(data, dict):
        return None
    for name in _TARGET_ARGS:
        value = data.get(name)
        if isinstance(value, list):
            value = tuple(str(v) for v in value) or None
        if value not in (None, ""):
            tag = request.get("provider_agent")
            confirm = request.get("assistant_confirm")
            asker = ((tag.get("worker_session_id") if isinstance(tag, dict) else None)
                     or (confirm.get("chat_session_id") if isinstance(confirm, dict) else None) or "")
            return (asker, tool, name, value if isinstance(value, tuple) else str(value))
    return None


@dataclass
class Ask:
    request_id: str
    summary: str
    delivered: bool = False
    marker: Any = None
    unclear: int = 0
    # The assistant's own question for this request (assistant_confirm), if any.
    confirm: Optional[Dict[str, Any]] = None
    # Same action on the same target (see retry_key); None when it has no target.
    retry: Optional[tuple] = None


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
        # retry_key -> monotonic time of the last approval on this call.
        self.approved: Dict[tuple, float] = {}
        # Retries of an approved action, approved by settle_retries().
        self.retries: "OrderedDict[str, Ask]" = OrderedDict()

    @property
    def awaiting(self) -> bool:
        """A question has been put to the caller and is waiting for an answer."""
        return self.current is not None and self.current.delivered

    def offer(self, request: Dict[str, Any]) -> Optional[str]:
        """Take a new pending request; return the note to put to the voice."""
        request_id = str(request.get("request_id") or "")
        if (not request_id or request_id in self.handled or request_id in self.queue
                or request_id in self.retries
                or (self.current and self.current.request_id == request_id)):
            return None
        summary = request_summary(request)
        reason = needs_screen(request)
        if reason:
            self.handled.add(request_id)
            return (f"A decision is waiting on screen and cannot be answered by voice. {reason} "
                    f"Tell the user in one sentence to answer it on screen: {summary}")
        confirm = request.get("assistant_confirm")
        ask = Ask(request_id, summary, confirm=confirm if isinstance(confirm, dict) else None,
                  retry=retry_key(request))
        approved_at = self.approved.get(ask.retry) if ask.retry else None
        if approved_at is not None and time.monotonic() - approved_at <= RETRY_WINDOW_SECONDS:
            # The caller already said yes to this action on this target: a
            # retry is approved by settle_retries(), not asked again.
            self.retries[request_id] = ask
            return None
        if self.current is not None:
            self.queue[request_id] = request
            return None
        self.current = ask
        return self.ask_text(self.current)

    async def settle_retries(self) -> list:
        """Approve the waiting retries of approved actions: ``[(request_id, note)]``.

        Each goes through the same authority checks and ``respond`` call as a
        spoken yes. The note is quiet context for the voice, not a question.
        """
        notes = []
        while self.retries:
            request_id, ask = self.retries.popitem(last=False)
            self.handled.add(request_id)
            text = await self._answer(ask, "allow", second_unclear=False, retry=True)
            notes.append((request_id, text))
        return notes

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
        self.retries.pop(request_id, None)
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

    async def _answer(self, ask: Ask, decision: Literal["allow", "deny"], *, second_unclear: bool,
                      retry: bool = False) -> str:
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
        logger.info("Voice approval %s for %s answered %s %s", ask.request_id, self.session_id, decision,
                    "as a retry of an action the caller approved" if retry else "by the caller's voice")
        await self._record_confirm(ask, decision)
        if decision == "allow" and ask.retry is not None:
            self.approved[ask.retry] = time.monotonic()
        if retry:
            return (f"Approved without asking again: the user already said yes to this action on the same "
                    f"target during this call: {ask.summary}. Do not ask about it.")
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
