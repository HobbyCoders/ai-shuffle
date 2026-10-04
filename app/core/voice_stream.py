"""Server-owned audio transports for GPT Live and fal.ai PersonaPlex.

The browser sends PCM16/24kHz mono, never provider credentials or tool events.
Delegation is dispatched by the server using the session's selected model.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from app.db import database
from app.db.async_database import run_sync
from app.core.voice_models import LIVE_MODEL
from app.core.voice_options import effective_options, validate_options
from app.core.voice_prompts import LIVE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS, PERSONAPLEX_INSTRUCTIONS

logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 48_000
MAX_TEXT_BYTES = 16_000
MAX_CONNECTIONS = 4
calls: dict[str, VoiceStream] = {}
_workers: set[asyncio.Task] = set()
# A handoff waits until the caller's transcript has been quiet this long, so
# words still in flight (or a continued sentence) join the same request.
SETTLE_QUIET_S = 0.7
SETTLE_MAX_S = 3.0
# Caller speech with no reply yet is held this long before a checkpoint stores
# it, so a pause to think does not split one request into two chat messages.
HOLD_UNANSWERED_S = 10.0
# A reply whose transcript stopped mid-sentence is held this long before a
# checkpoint stores it. The output transcript can stall for seconds inside one
# spoken reply (#1798: "I- I'll take a" ... 8 s ... "look when they come in.").
HOLD_MID_REPLY_S = 15.0
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*\s*$")
# Listening sounds GPT Live can make while the caller is still talking.
_BACKCHANNEL_WORDS = frozenset({
    "mm", "mmm", "mhm", "mmhmm", "hm", "hmm", "uh", "uhhuh", "huh", "um",
    "yeah", "yep", "yup", "ok", "okay", "right", "sure", "alright", "gotcha",
    "i", "see", "got", "it", "cool",
})


# Non-lexical sounds a transcript can contain when only noise or breath was heard.
_NOISE_WORDS = frozenset({"uh", "um", "umm", "hm", "hmm", "mm", "mmm", "mhm", "ah", "oh", "er", "erm", "huh"})


NO_REQUEST_NOTE = ("No words were heard for that handoff, so nothing was sent to the chat agent. "
                   "Do not report work. If the user did ask for something, ask them to repeat it.")
APPROVAL_ANSWER_NOTE = ("The caller's words were taken as their answer to the waiting approval question, "
                        "so nothing was sent to the chat agent. The app will report the decision. If they "
                        "asked for something else, ask them to repeat it once the approval is settled.")
# A spoken approval answer is heard once the caller has been quiet this long
# (or as soon as the voice starts replying, which means GPT Live heard the end
# of their turn). Longer than SETTLE_QUIET_S: "yes… but wait" must not approve.
APPROVAL_SETTLE_S = 1.5
# A natural pause for a held spoken note: nobody has spoken for this long, and
# longer when the caller spoke last and the voice has not answered yet.
PAUSE_QUIET_S = 1.5
PAUSE_AFTER_CALLER_S = 3.0
MERGED_REQUEST_NOTE = ("This handoff was combined with the request already sent to the chat agent "
                       "while the user was still speaking; its result will follow.")


def is_backchannel(text: str) -> bool:
    """True for an acknowledgment-only utterance such as "Mm-hmm." or "Yeah."."""
    words = re.findall(r"[a-z]+", text.lower().replace("-", ""))
    return 0 < len(words) <= 4 and all(word in _BACKCHANNEL_WORDS for word in words)


def is_noise(text: str) -> bool:
    """True when a caller transcript holds no words, only sounds like "Hmm." or "uh"."""
    return all(word in _NOISE_WORDS for word in re.findall(r"\w+", text.lower()))


def live_session_config(config: dict, history: list[dict]) -> dict:
    from app.core.query_options import decode_provider_model
    o = validate_options(LIVE_MODEL, config.get("options") or {})
    delegation = {"type": "client"}
    if o["delegation_type"] == "responses" and not config.get("conversation_surface"):
        decoded = decode_provider_model(o["delegation_model"])
        if not decoded:
            raise ValueError("Responses delegation needs a provider-bound model")
        backend = {
            "model": decoded[1],
            "instructions": o["delegation_instructions"] or DELEGATION_INSTRUCTIONS,
            # No app tools are exposed to provider-managed delegation.
            "tools": [],
        }
        if o["delegation_effort"]:
            backend["reasoning"] = {"effort": o["delegation_effort"]}
        delegation = {"type": "responses", "responses": backend}
    return {
        "model": LIVE_MODEL,
        "instructions": o["instructions"] or LIVE_INSTRUCTIONS,
        "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": config["voice"]}},
        "delegation": delegation,
        "input": history,
    }


def live_opening_event(text: str) -> dict:
    import uuid
    return {
        "type": "session.instructions.append", "event_id": str(uuid.uuid4()),
        "delegation_id": None,
        "content": "Greet immediately without waiting for the caller. Say these opening words in their written language, then pause and listen: " + text,
    }


def _turn_reply(session_id: str, turn_id: str) -> str:
    """Read only this handoff's public answer, never another turn or reasoning."""
    from app.db import session_events
    seq = 0
    parts = []
    while True:
        batch = session_events.get_events(session_id, from_seq=seq, limit=500)
        if not batch:
            break
        for event in batch:
            seq = max(seq, event["seq"])
            if event.get("turn_id") != turn_id:
                continue
            payload = event.get("payload") or {}
            kind = event.get("event_type")
            if kind == "tool.result":
                parts = []
            elif kind == "assistant.text_delta":
                parts.append(payload.get("text") or "")
            elif kind == "message.assistant" and payload.get("kind") in (None, "text"):
                parts.append(payload.get("content") or "")
    return "".join(parts).strip()


def commentary_chunks(text: str):
    """Stay below Live's 500-token append cap without silently losing a result."""
    chunk = ""
    for char in text:
        if len((chunk + char).encode("utf-8")) > 400:
            yield chunk
            chunk = ""
        chunk += char
    if chunk:
        yield chunk


async def delegate_to_selected_model(session_id: str, transcript: str, on_submitted=None) -> str:
    from app.core import api_user_scope, turn_manager
    from app.core.provider_agent import validate_provider_model
    from app.core.query_options import decode_provider_model

    row = await run_sync(database.get_session, session_id)
    selected = (row or {}).get("model")
    normal_conversation = row is not None and "mode" in row and row.get("mode") != "voice"
    if not selected and normal_conversation:
        profile = await run_sync(database.get_profile, row.get("profile_id")) or {}
        profile_config = profile.get("config") or {}
        if isinstance(profile_config, str):
            profile_config = json.loads(profile_config)
        selected = row.get("launched_model") or profile_config.get("model")
        if not selected and not row.get("provider_id"):
            from app.core.model_registry import DEFAULT_MODEL
            selected = DEFAULT_MODEL
    if not selected:
        return "No chat model is selected. Ask the user to choose one in the composer."
    decoded = decode_provider_model(selected)
    provider_id, model_id = decoded if decoded else (None, selected)
    if provider_id:
        validation = await run_sync(validate_provider_model, provider_id, model_id)
        if not validation.ok:
            return "The selected chat model is unavailable. Ask the user to choose another model."
    binding = row.get("voice_config") or {}
    if isinstance(binding, str):
        binding = json.loads(binding)
    # Only a live binding carries LiveOptions; a WebRTC binding stores the
    # realtime option set, which LiveOptions rejects as extra fields.
    instructions = DELEGATION_INSTRUCTIONS
    if binding.get("model") == LIVE_MODEL:
        options = validate_options(LIVE_MODEL, binding.get("options") or {})
        instructions = options["delegation_instructions"] or DELEGATION_INSTRUCTIONS
    # Submit to the SAME session: reuse its history/client and normal queue,
    # approval, stop, placement and model-switch semantics. No worker creation.
    overrides = {key: row.get(key) for key in ("effort", "permission_mode", "context_window", "fast")}
    if provider_id is None and normal_conversation:
        provider_id = row.get("provider_id")
    overrides.update(model=selected, provider=provider_id,
                     system_prompt_append=instructions)
    # VoiceStream flushes the spoken exchange before handing off. Keep the
    # context for the engine without adding a second giant user bubble.
    # ``voice_delegation``: this prompt was written by a voice model (the
    # Realtime ``submit_to_chat`` request) or wraps the call transcript (Live).
    # It is never the owner's own answer to an assistant_confirm question;
    # turn_manager skips it, and spoken approvals are decided from the caller's
    # audio transcript by app.core.voice_approval instead.
    payload: dict = {"user_message_already_recorded": True, turn_manager.VOICE_DELEGATION: True}
    # The delegated turn runs as the key that owns the session, never as the
    # owner (a key may open the audio socket on its own session).
    api_user_id = await run_sync(api_user_scope.session_api_user_id, row)
    if api_user_id:
        payload["api_user_id"] = api_user_id
    turn_id = await turn_manager.submit_async(
        session_id, "user", prompt=transcript, profile_id=row["profile_id"],
        project_id=row.get("project_id"), worktree_id=row.get("worktree_id"),
        overrides=overrides,
        payload=payload,
    )
    if on_submitted is not None:
        # The call-wide event subscriber follows progress, parking and later
        # wakes. Do not hold the handoff open or lose results after ten minutes.
        await on_submitted(turn_id)
        return ""
    settled = await turn_manager.wait_settled(turn_id, timeout=600)
    if settled is None or settled["state"] == "parked":
        return "Work is continuing in chat. No final result is available yet."
    if settled["state"] != "completed":
        return "The chat task was stopped or failed. Do not claim it succeeded. Check chat for details."
    return await run_sync(_turn_reply, session_id, turn_id) or "The chat task finished without a spoken result."


LIVE_URL = "wss://api.openai.com/v1/live/sessions"
# A dropped GPT Live connection is retried with backoff for about 20 seconds,
# long enough for a phone to move between Wi-Fi and mobile data. No attempt
# starts after RECONNECT_WINDOW_S; each attempt (connect plus session start)
# has its own limit, so the call ends at most about 30 seconds after the drop.
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0)
RECONNECT_WINDOW_S = 20
RECONNECT_ATTEMPT_S = 10
# Upper bound on restored connections in one call, so a provider that keeps
# dropping cannot hold a call in a reconnect loop.
MAX_RECONNECTS = 12
# Renew a Live session this long before the expires_at it reports, at a quiet
# moment if one comes, and no later than RENEW_LAST_S before expiry.
RENEW_LEAD_S = 120
RENEW_LAST_S = 15
RENEW_QUIET_S = 1.5
RENEW_MIN_AGE_S = 60
# Delegation results and progress notes sent while the connection is being
# restored wait for the new session. Microphone audio is never held.
MAX_HELD_EVENTS = 64
PROVIDER_CLOSE_RECONNECT = frozenset({"expired", "connection_lost"})
RESUME_NOTE = ("The voice connection was renewed in the middle of this call. Continue the "
               "conversation from where it left off. Do not greet the caller again.")
DROPPED_MESSAGE = ("The voice connection dropped and could not be restored. "
                   "Your conversation is saved; start the call again to keep talking.")
CONNECT_FAILED_MESSAGE = "Could not connect to the voice provider. Try starting the call again."


class VoiceConnectionLost(Exception):
    """The provider connection dropped and every reconnect attempt failed."""


def describe_close(exc: BaseException | None) -> str:
    """Name a failure for the log, with the WebSocket close codes and reasons.

    A dropped socket reads like "ConnectionClosedError: no close frame
    received or sent" (the TCP connection ended without a WebSocket close
    from either side) or "sent 1011 (internal error) keepalive ping timeout;
    no close frame received" (our keepalive gave up), which is what tells the
    causes apart afterwards.
    """
    if exc is None:
        return "no error"
    cause = exc.__cause__
    if isinstance(exc, VoiceConnectionLost) and cause is not None:
        return f"{exc} Last failure: {describe_close(cause)}"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


class VoiceStream:
    def __init__(self, websocket: WebSocket, row: dict, config: dict, api_key: str, max_minutes: int,
                 api_user_id: str | None = None):
        self.websocket = websocket
        self.session_id = row["id"]
        # The API key that opened the call (None for the owner): spoken
        # approvals re-check its access exactly as a tap on the card would.
        self.api_user_id = api_user_id
        self.approval_lock = asyncio.Lock()
        self.approval_consumed: set[int] = set()
        self.approval_task: asyncio.Task | None = None
        self.approval_now = False
        self.config = config
        self.options = effective_options(config)
        self.api_key = api_key
        self.max_seconds = max_minutes * 60
        self.started_at = time.time()
        self.connected = False
        self.closed = False
        self.upstream = None
        self.first_frame = None
        self.opening_event_id: str | None = None
        # Provider connection lifecycle: GPT Live reconnects and renewals.
        self.closed_event = asyncio.Event()
        self.restoring = False
        self.reconnects = 0
        self.held: deque[dict] = deque()
        self.stale_delegations: set[str] = set()
        self.expires_at: float | None = None
        self.provider_close_reason: str | None = None
        self.last_drop: BaseException | None = None
        # The browser's close code, logged when the browser leg ends the call.
        self.browser_close: str | None = None
        # Set once run() has cleaned up, so a reconnecting browser can wait
        # for this call's last words to be stored (take_over).
        self.finished = asyncio.Event()
        self.transcript: deque[dict] = deque(maxlen=200)
        self.pending = {"user": "", "assistant": ""}
        self.last_caption_at = 0.0
        self.caption_ids = {"user": 0, "assistant": 0}
        self.delegations: set[str] = set()
        self.delegation_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        # The pending caller text was typed into the call, not spoken.
        self.pending_typed = False
        self.last_caption_role = None
        self.progress = None
        self.transcript_seq = 0
        self.delegated_seq = 0
        self.last_user_caption_at = 0.0
        self.last_user_seq = 0
        # Delegation ids waiting for the caller's words to settle; None when idle.
        self.settling: list[str] | None = None

    @property
    def is_live(self):
        return self.config["model"] == LIVE_MODEL

    async def send(self, event: dict):
        if self.closed:
            return
        if self.is_live and (self.upstream is None or (self.restoring and event.get("type") != "session.start")):
            self.hold(event)
            return
        if not self.upstream:
            return
        did = event.get("delegation_id")
        if did is not None and did in self.stale_delegations:
            # The delegation belonged to a provider session that has since been
            # replaced, so its result goes to the new session as plain commentary.
            event = {**event, "delegation_id": None}
        try:
            await self.upstream.send(json.dumps(event))
        except ConnectionClosed:
            if not self.is_live:
                raise
            # The reader sees the same closure and restores the connection.
            self.hold(event)

    def hold(self, event: dict):
        """Keep a non-audio event for the next provider session."""
        if event.get("type") in ("session.input_audio.append", "session.start"):
            return
        if len(self.held) >= MAX_HELD_EVENTS:
            self.held.popleft()
            logger.warning("Voice %s dropped a held provider event while reconnecting", self.session_id)
        self.held.append(event)

    async def send_held(self):
        # Drain a snapshot: an event that fails again is held for the next session.
        pending = list(self.held)
        self.held.clear()
        for event in pending:
            await self.send(event)

    async def emit(self, event: dict):
        if not self.closed:
            async with self.send_lock:
                await self.websocket.send_json(event)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.connected = False
        self.closed_event.set()
        self.api_key = ""
        if self.upstream:
            try:
                await self.upstream.close()
            except Exception:  # A dead provider socket must not block hang-up.
                logger.debug("Voice %s: provider socket close failed", self.session_id, exc_info=True)
        try:
            await self.websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass

    def _is_mid_thought_backchannel(self) -> bool:
        """The reply so far is only a listening sound inside the caller's thought.

        A caller who had just asked a question may have been answered "Yeah.",
        so only speech that did not end in a question is joined.
        """
        user = self.pending["user"].rstrip()
        return bool(user) and not is_noise(user) and not user.endswith("?") and is_backchannel(self.pending["assistant"])

    async def flush(self, *, keep_reply: bool = False):
        """Store the pending exchange as one voice turn.

        ``keep_reply`` stores only the caller's words and keeps a reply that is
        still being spoken pending, so a handoff never cuts it in two (#1798).
        """
        from app.core.turn_recorder import record_voice_exchange
        # Checkpoint, handoff and speaker changes can flush concurrently. Keep
        # their durable turns in arrival order, including during barge-in.
        async with self.flush_lock:
            user, assistant = self.pending["user"], self.pending["assistant"]
            if keep_reply:
                assistant = ""
                self.pending = {"user": "", "assistant": self.pending["assistant"]}
            else:
                self.pending = {"user": "", "assistant": ""}
            typed, self.pending_typed = self.pending_typed, False
            if is_noise(user):
                user = ""  # Breath or background sound, not something the caller said.
            if user or assistant:
                self.caption_ids["user"] += 1
                if not keep_reply:
                    self.caption_ids["assistant"] += 1
                await record_voice_exchange(self.session_id, user or None, assistant or None, model=self.config["model"],
                                            user_source="typed" if typed else "voice")

    def is_quiet(self) -> bool:
        """A natural pause: nobody is mid-sentence, so a held note may be spoken."""
        if self.settling is not None or self.restoring:
            return False
        gap = PAUSE_AFTER_CALLER_S if self.last_caption_role == "user" else PAUSE_QUIET_S
        return time.monotonic() - self.last_caption_at >= gap

    def approval_waiting(self) -> bool:
        approvals = getattr(self.progress, "approvals", None)
        return bool(approvals is not None and approvals.awaiting)

    def listen_for_approval(self, *, now: bool = False) -> None:
        """Hear the caller's answer once their turn is over (see APPROVAL_SETTLE_S)."""
        if now:
            self.approval_now = True
        if self.approval_task is None or self.approval_task.done():
            self.approval_task = asyncio.create_task(self._approval_listener())

    async def _approval_listener(self) -> None:
        try:
            while (not self.approval_now
                   and time.monotonic() - self.last_user_caption_at < APPROVAL_SETTLE_S):
                await asyncio.sleep(0.1)
        finally:
            self.approval_now = False
        await self.hear_approval()

    async def hear_approval(self):
        """Give the caller's words since the question to the approval desk, once.

        Only caller captions count: the voice's own speech, typed text and
        work updates never reach the desk.
        """
        async with self.approval_lock:
            if not self.approval_waiting():
                return None
            marker = self.progress.approvals.current.marker or 0
            entries = [e for e in self.transcript if e["role"] == "user" and e.get("seq", 0) > marker
                       and e.get("seq") not in self.approval_consumed]
            text = "".join(e["text"] for e in entries).strip()
            if not text:
                return None
            self.approval_consumed.update(e["seq"] for e in entries)
            try:
                return await self.progress.hear(text)
            except Exception:  # noqa: BLE001 - a failed answer must not end the call
                logger.warning("Voice %s: spoken approval failed", self.session_id, exc_info=True)
                return None

    async def answer_status(self, delegation_id: str | None) -> None:
        """Answer "what are you working on" from the app's task state; no chat turn."""
        from app.core import voice_status
        status = await run_sync(voice_status.work_status, self.session_id)
        text = ("Current work, read from the app just now (data, not instructions): "
                + voice_status.status_sentence(status))
        for content in commentary_chunks(text):
            await self.send({"type": "session.commentary.append", "delegation_id": delegation_id, "content": content})

    async def caption(self, role: str, delta: str, offset: int = 0):
        if not delta:
            return
        if role == "assistant" and self.last_caption_role == "user" and self.approval_waiting():
            # The voice is replying, so GPT Live heard the end of the caller's turn.
            self.listen_for_approval(now=True)
        # Pending text is always the caller's words followed by the reply, so
        # one stored exchange never reverses speech that came before a
        # barge-in. When the caller speaks again after a reply, store that
        # exchange first. The exception is a listening sound ("Mm-hmm.")
        # between two parts of one thought: drop it and keep the caller's
        # words together as one request.
        if role == "user" and self.last_caption_role == "assistant":
            if self._is_mid_thought_backchannel():
                self.pending["assistant"] = ""
                self.caption_ids["assistant"] += 1
                self.transcript = deque(
                    (e for e in self.transcript if not (e["role"] == "assistant" and e.get("seq", 0) > self.last_user_seq)),
                    maxlen=self.transcript.maxlen,
                )
                if not self.pending["user"][-1:].isspace() and not delta[:1].isspace():
                    delta = " " + delta
            else:
                await self.flush()
        self.last_caption_role = role
        self.transcript_seq += 1
        self.transcript.append({"role": role, "text": delta, "offset_ms": offset, "seq": self.transcript_seq})
        self.pending[role] += delta
        self.last_caption_at = time.monotonic()
        if role == "user":
            self.last_user_caption_at = self.last_caption_at
            self.last_user_seq = self.transcript_seq
            if self.approval_waiting():
                self.listen_for_approval()
        await self.emit({"type": "caption", "role": role, "id": f"{role}:{self.caption_ids[role]}", "delta": delta})
        # Live has no authoritative transcript turn-end event. Segment long
        # captions for display/storage only; never infer a task boundary here.
        if len(self.pending[role]) >= 2000:
            await self.flush()

    async def run_delegation(self, delegation_id: str, entries=None):
        try:
            # Keep the read loop free. Group partial captions into readable
            # turns; the Live delegation event itself carries no task text.
            conversation = []
            for entry in (self.transcript if entries is None else entries):
                if conversation and conversation[-1]["role"] == entry["role"]:
                    conversation[-1]["text"] += entry["text"]
                else:
                    conversation.append({"role": entry["role"], "text": entry["text"]})
            context = "Live conversation context (only the latest new request needs action):\n" + "".join(
                f"{entry['role']}: {entry['text'].strip()}\n" for entry in conversation
            )
            if not any(e["role"] == "user" and not is_noise(e["text"]) for e in conversation):
                # Noise, a breath or an empty transcript: no request for the
                # chat agent, and nothing worth saying aloud either.
                await self.send({"type": "session.thinking.append", "delegation_id": delegation_id,
                                 "content": NO_REQUEST_NOTE})
                return
            # Store the request before the chat turn starts, but not the reply
            # the voice is speaking right now: it is stored whole once spoken.
            await self.flush(keep_reply=True)
            if self.progress is not None:
                async def submitted(turn_id):
                    await self.progress.submitted(turn_id, delegation_id)
                result = await delegate_to_selected_model(self.session_id, context, on_submitted=submitted)
                if not result:
                    return  # Progress and final results arrive on the event stream.
            else:
                result = await delegate_to_selected_model(self.session_id, context)
            # Live appends have a 500-token ceiling. <=400 UTF-8 bytes is a
            # conservative bound even without the provider's tokenizer.
            for content in commentary_chunks(result or "The chat task returned no result."):
                await self.send({"type": "session.commentary.append", "delegation_id": delegation_id, "content": content})
        except Exception:
            logger.exception("Voice delegation failed for %s", self.session_id)
            await self.send({"type": "session.commentary.append", "delegation_id": delegation_id,
                             "content": "The delegated task failed. No successful result was returned."})

    async def settle_speech(self):
        """Wait briefly for the caller's transcript to go quiet.

        Live opens a handoff when it decides the request is complete, but the
        last words' transcript can still be in flight, and the caller may carry
        on. Waiting keeps the whole request in one chat turn.
        """
        deadline = time.monotonic() + SETTLE_MAX_S
        while True:
            now = time.monotonic()
            remaining = min(SETTLE_QUIET_S - (now - self.last_user_caption_at), deadline - now)
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def dispatch_when_settled(self):
        try:
            await self.settle_speech()
        finally:
            ids, self.settling = self.settling or [], None
        # Capture only speech since the previous handoff. The chat already
        # owns the earlier history.
        entries = [e for e in self.transcript if e.get("seq", 0) > self.delegated_seq]
        self.delegated_seq = self.transcript_seq
        for extra in ids[1:]:
            try:
                await self.send({"type": "session.thinking.append", "delegation_id": extra, "content": MERGED_REQUEST_NOTE})
            except Exception:
                logger.warning("Voice could not acknowledge a combined handoff for %s", self.session_id)
        # An answer to a waiting approval question is the app's to decide from
        # the caller's words; it is never sent to the chat agent as a request.
        if self.approval_waiting():
            await self.hear_approval()
        if any(e.get("seq") in self.approval_consumed for e in entries):
            entries = [e for e in entries if e.get("seq") not in self.approval_consumed]
            if not any(e["role"] == "user" and not is_noise(e["text"]) for e in entries):
                await self.send({"type": "session.thinking.append", "delegation_id": ids[0],
                                 "content": APPROVAL_ANSWER_NOTE})
                return
        if self.progress is not None:
            from app.core.voice_status import is_status_question
            spoken = "".join(e["text"] for e in entries if e["role"] == "user")
            if is_status_question(spoken):
                # A quick status question reads the app's task state directly.
                await self.answer_status(ids[0])
                return
        await self.run_delegation(ids[0], entries)

    async def upstream_frames(self):
        if self.first_frame is not None:
            yield self.first_frame
            self.first_frame = None
        async for frame in self.upstream:
            yield frame

    async def prime_fal(self):
        """Warm the model with silence before inviting the user to speak.

        A cold GPU can take tens of seconds. Sending live microphone audio
        during that interval would leave a long, stale speech backlog.
        """
        import msgpack
        async def silence():
            for _ in range(150):
                await self.upstream.send(msgpack.packb({
                    "audio": bytes(3840), "voice": self.config["voice"],
                    "prompt": self.options["instructions"] or PERSONAPLEX_INSTRUCTIONS,
                }))
                await asyncio.sleep(.08)
        task = asyncio.create_task(silence())
        try:
            self.first_frame = await asyncio.wait_for(self.upstream.recv(), 60)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def receive_upstream(self):
        import msgpack
        first = True
        async for raw in self.upstream_frames():
            if first:
                logger.info("Voice %s received its first provider frame (%d bytes)", self.session_id, len(raw))
                first = False
            if self.is_live:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.output_audio.delta":
                    audio = base64.b64decode(event.get("delta") or "", validate=True)
                    if audio:
                        async with self.send_lock:
                            await self.websocket.send_bytes(audio)
                elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                    role = "user" if kind == "session.input_transcript.delta" else "assistant"
                    await self.caption(role, event.get("delta") or "", event.get("end_ms") or 0)
                elif kind == "session.instructions.appended" and self.opening_event_id and event.get("client_event_id") == self.opening_event_id:
                    self.opening_event_id = None
                    await self.send({"type": "session.commentary.append", "delegation_id": None,
                                     "content": "Begin the conversation now, following the instructions provided."})
                elif kind == "session.delegation.created":
                    if self.options["delegation_type"] == "responses" and not self.config.get("conversation_surface"):
                        continue  # Provider-owned: never also dispatch an app worker.
                    delegation = event.get("delegation") or {}
                    did = delegation.get("id")
                    if not isinstance(did, str) or not did or did in self.delegations:
                        continue
                    self.delegations.add(did)
                    if len(self.delegations) > 200:
                        raise ValueError("Voice delegation limit reached; start a new call.")
                    if self.settling is not None:
                        # Another handoff while the caller's words are still
                        # settling is the same request: one chat turn for both.
                        self.settling.append(did)
                    elif len(_workers) >= 8:
                        await self.send({"type": "session.commentary.append", "delegation_id": did,
                                         "content": "The backend is busy with another task. Ask the user to repeat this new request after it finishes."})
                    else:
                        self.settling = [did]
                        self.delegation_task = asyncio.create_task(self.dispatch_when_settled())
                        _workers.add(self.delegation_task)
                        self.delegation_task.add_done_callback(_workers.discard)
                elif kind in ("error", "session.error"):
                    raise ValueError(str((event.get("error") or {}).get("message") or "The Live API rejected an event."))
                elif kind == "session.closed":
                    self.provider_close_reason = str(event.get("reason") or (event.get("session") or {}).get("reason") or "")
                    return
            else:
                event = msgpack.unpackb(raw, raw=False) if isinstance(raw, bytes) else json.loads(raw)
                if event.get("error"):
                    raise ValueError("fal.ai rejected the audio stream.")
                audio = event.get("audio")
                if isinstance(audio, str):
                    audio = base64.b64decode(audio, validate=True)
                if audio:
                    async with self.send_lock:
                        await self.websocket.send_bytes(audio)
                await self.caption("assistant", event.get("text") or "")

    async def receive_text(self, raw: str):
        """Admit user input only, never browser-supplied provider/tool events."""
        import re
        # JSON escaping may expand each text byte to a six-byte \uXXXX escape.
        if len(raw.encode("utf-8")) > MAX_TEXT_BYTES * 6 + 512:
            raise ValueError("Voice text frame is too large.")
        event = json.loads(raw)
        if (not isinstance(event, dict) or set(event) != {"type", "id", "text"}
                or event["type"] != "input_text" or not isinstance(event["id"], str)
                or not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", event["id"])
                or not isinstance(event["text"], str)):
            raise ValueError("Only PCM audio frames or user text are accepted.")
        text, identity = event["text"].strip(), event["id"]
        async def reject(message):
            await self.emit({"type": "text.rejected", "id": identity, "message": message})
        if not text or len(event["text"].encode("utf-8")) > MAX_TEXT_BYTES:
            await reject("Voice messages must contain text and be at most 16,000 bytes. Your draft has been kept.")
            return
        if not self.is_live or (self.options["delegation_type"] == "responses" and not self.config.get("conversation_surface")):
            await reject("This voice engine does not support text or attachments. End the call to send your draft in chat.")
            return
        key = "typed-" + identity
        if key not in self.delegations:
            if len(_workers) >= 8 or len(self.delegations) >= 200:
                await reject("The voice backend is busy. Keep your draft and try again shortly.")
                return
            # Typed requests reuse the existing same-session delegation
            # path, not a guessed provider text event or a second conversation.
            await self.flush()
            await self.caption("user", "\n\nTyped message:\n" + text)
            self.pending_typed = True  # Stored as typed text, not as speech.
            await self.flush()  # Durable admission before acknowledging/dispatching.
            entries = [e for e in self.transcript if e.get("seq", 0) > self.delegated_seq]
            self.delegated_seq = self.transcript_seq
            self.delegations.add(key)
            self.delegation_task = asyncio.create_task(self.run_delegation(None, entries))
            _workers.add(self.delegation_task)
            self.delegation_task.add_done_callback(_workers.discard)
        await self.emit({"type": "text.accepted", "id": identity})

    async def receive_browser(self):
        import msgpack
        audio_bytes = 0
        audio_started = time.monotonic()
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                reason = message.get("reason")
                self.browser_close = f"code {message.get('code')}" + (f", reason {reason!r}" if reason else "")
                return
            data = message.get("bytes")
            if data is None:
                await self.receive_text(message.get("text") or "")
                continue
            if not data or len(data) > MAX_FRAME_BYTES or len(data) % 2:
                raise ValueError("Invalid PCM audio frame.")
            if audio_bytes == 0:
                logger.info("Voice %s received microphone PCM (%d bytes)", self.session_id, len(data))
            audio_bytes += len(data)
            if audio_bytes > 48_000 * (time.monotonic() - audio_started + 5):
                raise ValueError("Audio arrived faster than realtime.")
            if self.is_live:
                await self.send({"type": "session.input_audio.append", "audio": base64.b64encode(data).decode()})
            else:
                await self.upstream.send(msgpack.packb({
                    "audio": data, "voice": self.config["voice"],
                    "prompt": self.options["instructions"] or PERSONAPLEX_INSTRUCTIONS,
                }))

    async def checkpoint(self):
        while True:
            await asyncio.sleep(10)
            if self.checkpoint_due():
                await self.flush()

    def checkpoint_due(self) -> bool:
        # Speech nobody has answered yet is usually a pause to think; storing
        # it now would split one request into two chat messages.
        unanswered = self.pending["user"] and not self.pending["assistant"]
        # Likewise a reply that stopped mid-sentence is usually still being
        # spoken; storing it now would split one reply into two messages.
        mid_reply = self.pending["assistant"] and not _SENTENCE_END.search(self.pending["assistant"])
        hold = HOLD_UNANSWERED_S if unanswered else HOLD_MID_REPLY_S if mid_reply else 2
        return time.monotonic() - self.last_caption_at >= hold

    async def start_live(self, history: list[dict]) -> None:
        live_config = live_session_config(self.config, history)
        if self.config.get("conversation_surface"):
            from app.core.voice_bridge import conversation_instructions
            row = await run_sync(database.get_session, self.session_id)
            overlay = await run_sync(conversation_instructions, row)
            live_config["instructions"] += "\n\n" + overlay
        await self.send({"type": "session.start", "session": live_config})
        event = json.loads(await asyncio.wait_for(self.upstream.recv(), 20))
        if event.get("type") != "session.started":
            raise ValueError(str((event.get("error") or {}).get("message") or "GPT Live did not start."))
        expires_at = (event.get("session") or {}).get("expires_at")
        self.expires_at = float(expires_at) if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) else None
        logger.info("Voice %s: GPT Live session started (expires in %s s)", self.session_id,
                    int(self.expires_at - time.time()) if self.expires_at else "unknown")
        if self.restoring:
            return  # Never greet again after a reconnect.
        if self.options["opening"] and not history and self.config.get("greet", True):
            # Provider rejection is surfaced by receive_upstream, not silently
            # treated as spoken audio. Never greet again after conversation replay.
            event = live_opening_event(self.options["opening"])
            self.opening_event_id = event["event_id"]
            await self.send(event)

    async def live_history(self, resumed: bool = False) -> list[dict]:
        """The saved conversation as GPT Live start-up history.

        A reconnect rebuilds it from everything stored so far, including this
        call's own exchanges, so the new provider session carries on the talk.
        """
        from app.core.voice_bridge import build_context_seed
        seed = await run_sync(build_context_seed, self.session_id)
        if resumed:
            seed = (seed + "\n\n" if seed else "") + RESUME_NOTE
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": seed}]}] if seed else []

    async def renew_when_due(self):
        """Return when the Live session should be replaced before it expires."""
        expires_at = self.expires_at or 0.0
        if expires_at - time.time() < RENEW_MIN_AGE_S + RENEW_LAST_S:
            await self.closed_event.wait()  # Implausibly short; never churn sessions.
            return
        await asyncio.sleep(max(RENEW_MIN_AGE_S, expires_at - RENEW_LEAD_S - time.time()))
        # Prefer a pause in the conversation over cutting off a sentence.
        while time.time() < expires_at - RENEW_LAST_S and time.monotonic() - self.last_caption_at < RENEW_QUIET_S:
            await asyncio.sleep(0.25)

    def reconnect_reason(self, reader: asyncio.Task, done: set) -> str | None:
        """Why the provider connection should be restored, or None to end the call."""
        if self.closed or not self.is_live:
            return None
        if reader not in done:
            return "renew"
        if reader.cancelled():
            return None
        exc = reader.exception()
        if isinstance(exc, ConnectionClosed) and not isinstance(exc, ConnectionClosedOK):
            return "dropped"
        if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):
            return "dropped"
        if exc is None and self.provider_close_reason in PROVIDER_CLOSE_RECONNECT:
            return self.provider_close_reason
        return None

    async def wait_closed(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self.closed_event.wait(), delay)
        except TimeoutError:
            pass
        return self.closed

    async def discard_upstream(self, old, graceful: bool):
        try:
            async with asyncio.timeout(3):
                if graceful:
                    await old.send(json.dumps({"type": "session.close"}))
                await old.close()
        except Exception:  # The old session is being replaced either way.
            logger.debug("Voice %s: closing the replaced provider socket failed", self.session_id, exc_info=True)

    async def restore_session(self, history: list[dict]):
        upstream = await connect(LIVE_URL, additional_headers={"Authorization": "Bearer " + self.api_key},
                                 open_timeout=RECONNECT_ATTEMPT_S, max_size=2_000_000)
        if self.closed:
            await upstream.close()
            return
        self.upstream = upstream
        try:
            await self.start_live(history)  # self.restoring: no greeting
        except BaseException:
            self.upstream = None
            await self.discard_upstream(upstream, graceful=False)
            raise

    async def reconnect(self, reason: str, reader: asyncio.Task | None = None) -> None:
        """Replace the GPT Live session and carry the call on.

        Raises VoiceConnectionLost when every attempt fails. Returns early,
        without a new session, when the call is hung up meanwhile.
        """
        old, self.upstream = self.upstream, None
        self.connected = False
        self.restoring = True
        self.reconnects += 1
        # Delegations of the old session do not exist in the new one; their
        # results are delivered as plain commentary instead.
        self.stale_delegations |= self.delegations
        detail = ""
        if reader is not None and reader.done() and not reader.cancelled() and reader.exception() is not None:
            detail = f" ({describe_close(reader.exception())})"
        elif reason in PROVIDER_CLOSE_RECONNECT:
            detail = f" (provider session.closed, reason {reason})"
        logger.warning("Voice %s: GPT Live connection %s after %d s%s; reconnecting (%d this call)",
                       self.session_id, reason, int(time.time() - self.started_at), detail, self.reconnects)
        self.provider_close_reason = None
        self.expires_at = None
        # A planned renewal is usually seamless; the caller only sees a
        # reconnect when the connection dropped or a renewal attempt failed.
        announced = reason != "renew"
        if announced:
            await self.tell_browser("reconnecting")
        dropped_at = time.monotonic()
        try:
            if old is not None:
                await self.discard_upstream(old, graceful=reason == "renew")
            if self.reconnects > MAX_RECONNECTS:
                raise VoiceConnectionLost(f"Too many reconnects in one call ({MAX_RECONNECTS}).")
            await self.flush()  # The rebuilt history includes everything said so far.
            history = await self.live_history(resumed=True)
            for attempt, delay in enumerate(RECONNECT_DELAYS, 1):
                if attempt > 1 and time.monotonic() - dropped_at + delay > RECONNECT_WINDOW_S:
                    break  # No new attempt starts after the window.
                if await self.wait_closed(delay):
                    return
                try:
                    async with asyncio.timeout(RECONNECT_ATTEMPT_S):
                        await self.restore_session(history)
                except Exception as exc:  # noqa: BLE001 - every failure is retried, bounded
                    self.last_drop = exc
                    logger.warning("Voice %s: reconnect attempt %d failed after %.1f s: %s", self.session_id,
                                   attempt, time.monotonic() - dropped_at, describe_close(exc))
                    if not announced:
                        announced = True
                        await self.tell_browser("reconnecting")
                    continue
                if self.closed:
                    return
                self.restoring = False
                self.connected = True
                logger.info("Voice %s: GPT Live connection restored on attempt %d after %.1f s", self.session_id,
                            attempt, time.monotonic() - dropped_at)
                if announced:
                    await self.tell_browser("live")
                await self.send_held()
                return
            raise VoiceConnectionLost("Every reconnect attempt failed.") from self.last_drop
        finally:
            self.restoring = False

    async def tell_browser(self, state: str) -> None:
        """Show the caller that the provider connection is being restored.

        Microphone audio the browser sends meanwhile is dropped, not replayed.
        """
        try:
            await self.emit({"type": "status", "state": state})
        except (RuntimeError, WebSocketDisconnect, OSError):
            pass  # The browser leg ends the call on its own.

    async def report_quiet_end(self, *, upstream_ended: bool) -> None:
        """Say who ended a call that stopped without an error (#1671).

        A provider that closes its session cleanly used to reach the browser as
        an ordinary close, which the client could not tell apart from the user
        pressing End. Name the cause in the log and to the browser.
        """
        if upstream_ended:
            logger.info("Voice %s: the provider ended the session", self.session_id)
            try:
                await self.emit({"type": "ended", "reason": "provider-closed"})
            except (RuntimeError, WebSocketDisconnect):
                pass
        else:
            logger.info("Voice %s: the browser closed the audio connection (%s)", self.session_id,
                        self.browser_close or "no close code")

    async def run(self):
        tasks = []
        provider = []
        url = LIVE_URL if self.is_live else "wss://fal.run/fal-ai/personaplex/realtime"
        auth = ("Bearer " if self.is_live else "Key ") + self.api_key
        if not self.is_live:
            self.api_key = ""  # PersonaPlex is never reconnected; Live keeps it to reconnect.
        budget = asyncio.timeout(self.max_seconds)
        try:
            async with budget:
                upstream = await connect(url, additional_headers={"Authorization": auth}, open_timeout=45, max_size=2_000_000)
                if self.closed:
                    await upstream.close()
                    return
                self.upstream = upstream
                if self.is_live:
                    history = await self.live_history()
                    if history:
                        self.transcript.append({"role": "history", "text": history[0]["content"][0]["text"], "offset_ms": 0})
                    if self.options["delegation_type"] != "responses" or self.config.get("conversation_surface"):
                        from app.core.voice_progress import VoiceProgress
                        approvals = None
                        if self.config.get("conversation_surface"):
                            # Approvals are asked aloud and answered by the caller's voice.
                            from app.core.voice_approval import SpokenApprovals
                            approvals = SpokenApprovals(self.session_id, api_user_id=self.api_user_id,
                                                        marker=lambda: self.transcript_seq)
                        self.progress = VoiceProgress(self.session_id, self.send, quiet=self.is_quiet,
                                                      approvals=approvals)
                        await self.progress.open()
                    await self.start_live(history)
                else:
                    await self.prime_fal()
                self.connected = True
                if not self.config.get("conversation_surface"):
                    await run_sync(database.update_session, self.session_id, launched_model=self.config["model"])
                await run_sync(database.set_session_active, self.session_id, True)
                await self.emit({"type": "ready", "model": self.config["model"], "sample_rate": 24000})
                tasks = [asyncio.create_task(self.receive_browser()), asyncio.create_task(self.checkpoint())]
                if self.progress is not None:
                    tasks.append(asyncio.create_task(self.progress.run()))
                while True:
                    reader = asyncio.create_task(self.receive_upstream())
                    provider = [reader]
                    if self.is_live and self.expires_at:
                        provider.append(asyncio.create_task(self.renew_when_due()))
                    done, _ = await asyncio.wait(tasks + provider, return_when=asyncio.FIRST_COMPLETED)
                    for task in provider:
                        if task not in done:
                            task.cancel()
                    await asyncio.gather(*provider, return_exceptions=True)
                    reason = None if done & set(tasks) else self.reconnect_reason(reader, done)
                    if reason:
                        restore = asyncio.create_task(self.reconnect(reason, reader))
                        provider = [restore]
                        done, _ = await asyncio.wait(tasks + provider, return_when=asyncio.FIRST_COMPLETED)
                        if restore in done:
                            restore.result()  # VoiceConnectionLost when every attempt failed.
                            if self.closed:
                                return  # Hung up while reconnecting.
                            continue
                        restore.cancel()
                        await asyncio.gather(restore, return_exceptions=True)
                    for task in done:
                        task.result()
                    await self.report_quiet_end(upstream_ended=reader in done)
                    return
        except TimeoutError:
            if budget.expired():
                from app.core.voice_bridge import registry
                await run_sync(registry.mark_capped, self.session_id, self.max_seconds // 60)
                message = "This call reached its time limit. Start a new call."
            else:
                message = "The voice provider timed out while connecting. Try again."
            await self.emit({"type": "error", "message": message})
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:
            # The close codes and reasons say which side ended the socket and
            # why ("no close frame received or sent" is a TCP-level drop).
            logger.warning("Voice audio stream failed for %s after %d s: %s", self.session_id,
                           int(time.time() - self.started_at), describe_close(exc))
            # A dropped socket's library text ("no close frame received or
            # sent") means nothing to a caller; name what happened instead.
            message = str(exc)[:300]
            if isinstance(exc, (VoiceConnectionLost, ConnectionClosed, OSError)):
                message = DROPPED_MESSAGE if tasks else CONNECT_FAILED_MESSAGE
            try:
                await self.emit({"type": "error", "message": message})
            except (RuntimeError, WebSocketDisconnect, OSError):
                pass  # The browser leg is gone as well; nobody is left to tell.
        finally:
            try:
                if self.approval_task is not None:
                    provider.append(self.approval_task)
                for task in tasks + provider:
                    task.cancel()
                await asyncio.gather(*tasks, *provider, return_exceptions=True)
                if self.progress is not None:
                    self.progress.close()
                await self.close()
                await self.flush()
                if calls.get(self.session_id) is self:
                    calls.pop(self.session_id, None)
                    if not self.config.get("conversation_surface"):
                        await run_sync(database.set_session_active, self.session_id, False)
            finally:
                self.finished.set()


async def stop(session_id: str) -> bool:
    call = calls.get(session_id)
    if call:
        await call.close()
    return bool(call)


TAKE_OVER_WAIT_S = 10
_closing: set[asyncio.Task] = set()


async def take_over(session_id: str) -> None:
    """End a session's leftover call so a reconnecting browser can replace it.

    A browser whose audio socket dropped reopens it with ``resume``. The
    server may not have noticed the drop yet (a dead TCP connection can look
    open for minutes, and the browser socket has no keepalive pings), so the
    old call would refuse the new socket and keep its provider session open.
    Waits, bounded, for the old call to store its last words so the new
    provider session's history includes them.
    """
    call = calls.get(session_id)
    if call is None:
        return
    logger.info("Voice %s: a reconnecting browser replaces the previous audio connection", session_id)
    closing = asyncio.create_task(call.close())
    _closing.add(closing)
    closing.add_done_callback(_closing.discard)
    try:
        await asyncio.wait_for(call.finished.wait(), TAKE_OVER_WAIT_S)
    except TimeoutError:
        logger.warning("Voice %s: the previous audio connection did not finish within %d s", session_id, TAKE_OVER_WAIT_S)


def state(session_id: str) -> dict | None:
    call = calls.get(session_id)
    if not call:
        return None
    return {"active": not call.closed, "connected": call.connected, "started_at": call.started_at,
            "model": call.config["model"], "active_tasks": call.progress.active_count if call.progress is not None else int(bool(call.delegation_task and not call.delegation_task.done()))}
