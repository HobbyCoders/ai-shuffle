"""Server-side sideband bridge for voice sessions (wave VW3, ruling VM8).

Transport shape (ruling VM2): the browser holds a WebRTC leg straight to
OpenAI — audio never transits this process — and the backend joins the SAME
realtime call over a *sideband* WebSocket
(``wss://api.openai.com/v1/realtime?call_id=...``) authenticated with the real
provider API key. That leg is where the backend owns instructions, VAD config
and (from VW4) the tool declarations the client can neither see nor tamper
with, and where both sides' final transcripts arrive for durable persistence.

This module ships:

* :class:`VoiceBridge` — one live call. Opens the sideband, sends the initial
  ``session.update``, reads events until the socket closes, assembles complete
  exchanges (one user utterance + the assistant reply that follows it) and
  hands each one to :func:`app.core.turn_recorder.record_voice_exchange`.
* :class:`VoiceCallRegistry` — at most ONE live bridge per session, guarded by
  an asyncio lock. Posting a new ``call_id`` REPLACES the existing attachment
  (old one is closed first); ``shutdown_all`` is wired into the app lifespan.

Deliberately NOT here (scope fences):

* No server-side reconnect. When the sideband drops, the bridge simply marks
  itself disconnected — reconnect is FE-driven with a fresh ``call_id``, and
  the replace-attach path reseeds context (see :func:`build_context_seed`).
* No tools / agent dispatch — that is wave VW4, which replaces
  :data:`VOICE_INSTRUCTIONS_V1` with the delegation-first coordinator prompt.

Robustness rule (mirrors the recorder): a bridge bug must never take down the
call or the request that started it — the read loop swallows and logs per-event
failures, and persistence failures never propagate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# The sideband endpoint: same realtime call as the browser's WebRTC leg, joined
# by call_id. Authenticated with the provider row's real API key.
SIDEBAND_URL = "wss://api.openai.com/v1/realtime"

# Placeholder V1 coordinator prompt. VW4 REPLACES this constant with the
# delegation-first instruction contract (ruling VM16) once the dispatch tools
# exist — keep the name stable so that wave can find it.
VOICE_INSTRUCTIONS_V1 = (
    "You are the ai-shuffle voice assistant. You are talking with the user out "
    "loud, so keep every answer short, natural and conversational — a sentence "
    "or two unless more is genuinely needed, no markdown, no lists, no code "
    "read-outs. Be friendly and direct. You have no tools yet: if the user asks "
    "for work that needs files, commands or an agent, say plainly that hands-on "
    "work is not wired up yet and offer to talk it through instead. Never invent "
    "results you did not actually produce."
)

# Turn-taking config sent on attach. Semantic VAD keeps the channel always-open
# (interaction contract §1: no push-to-talk) and lets the user barge in.
TURN_DETECTION_CONFIG: Dict[str, Any] = {
    "type": "semantic_vad",
    "eagerness": "medium",
    "interrupt_response": True,
    "create_response": True,
}

# Context seeding bounds — "last ~20 exchanges", hard-capped by chars so a long
# session can never blow up the instruction payload.
CONTEXT_SEED_MAX_MESSAGES = 40
CONTEXT_SEED_MAX_CHARS = 4000
CONTEXT_SEED_HEADER = "Conversation so far:"

# Per-utterance transcript cap — a runaway transcript must not become an
# unbounded row.
TRANSCRIPT_MAX_CHARS = 32768

# Scaling factor for the call-duration cap. A module constant purely so tests
# can shrink the deadline to milliseconds without waiting on real minutes.
SECONDS_PER_MINUTE = 60.0


# ============================================================================
# Sideband transport
# ============================================================================

async def _open_sideband(call_id: str, api_key: str):
    """Open the authenticated sideband WebSocket for ``call_id``.

    Split out as a module-level function so tests can substitute an in-process
    fake — nothing else in this module knows about the ``websockets`` package.
    """
    # Imported lazily: the package ships with uvicorn[standard], but a missing
    # optional extra must fail the call, not the whole app import.
    from websockets.asyncio.client import connect

    url = f"{SIDEBAND_URL}?call_id={call_id}"
    return await connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
    )


# ============================================================================
# Context seeding
# ============================================================================

def build_context_seed(session_id: str) -> str:
    """Compact recap of the session's recent history, or ``""`` when empty.

    Synchronous (call via ``run_sync``). This ONE mechanism covers both cases
    that need history: rejoining a session days later, and the FE's reconnect
    near OpenAI's session wall — a reconnect re-attaches with a fresh
    ``call_id``, and the replace path rebuilds the seed from everything that has
    been persisted so far, including the exchanges of the call being replaced.
    """
    try:
        rows = database.get_session_messages(session_id)
    except Exception:
        logger.warning(
            "Voice: could not read history to seed session %s", session_id,
            exc_info=True,
        )
        return ""

    lines: List[str] = []
    for row in rows[-CONTEXT_SEED_MAX_MESSAGES:]:
        role = row.get("role")
        content = (row.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        lines.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")

    # Budget from the END: the most recent turns are the ones worth keeping.
    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > CONTEXT_SEED_MAX_CHARS:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return ""
    kept.reverse()
    return CONTEXT_SEED_HEADER + "\n" + "\n".join(kept)


# ============================================================================
# The bridge
# ============================================================================

class VoiceBridge:
    """One live sideband attachment for one voice session."""

    def __init__(
        self,
        session_id: str,
        call_id: str,
        *,
        api_key: str,
        model: str,
        max_call_minutes: int,
    ) -> None:
        self.session_id = session_id
        self.call_id = call_id
        self.model = model
        self.max_call_minutes = max_call_minutes
        self._api_key = api_key

        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.connected = False
        self.stop_reason: Optional[str] = None

        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._deadline_task: Optional[asyncio.Task] = None
        self._stopped = False

        # In-flight exchange assembly.
        self._pending_user: List[str] = []
        self._pending_assistant: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether this bridge is still the session's live attachment."""
        return not self._stopped

    def state(self) -> Dict[str, Any]:
        """The ``GET /state`` payload for this bridge."""
        return {
            "active": self.active,
            "connected": self.connected,
            "started_at": self.started_at,
            "model": self.model,
        }

    async def start(self) -> None:
        """Connect, configure the realtime session, and start reading events.

        Raises whatever the transport raises — the caller turns a failed connect
        into a 502 rather than registering a dead bridge.
        """
        self._ws = await _open_sideband(self.call_id, self._api_key)
        self.connected = True
        self.started_at = time.time()
        logger.info(
            "Voice: sideband attached for session %s (call %s, model %s)",
            self.session_id, self.call_id, self.model,
        )

        # A rejected/failed session.update leaves the call running on OpenAI's
        # defaults, which is far better than dropping the user's call.
        try:
            await self._send_session_update()
        except Exception:
            logger.warning(
                "Voice: session.update failed for %s (call continues on defaults)",
                self.session_id, exc_info=True,
            )

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"voice-sideband-{self.session_id}"
        )
        self._deadline_task = asyncio.create_task(
            self._duration_cap(), name=f"voice-deadline-{self.session_id}"
        )

    async def stop(self, reason: str = "hangup") -> None:
        """Close the sideband and settle the bridge. Idempotent; never raises."""
        if self._stopped:
            return
        self._stopped = True
        self.stop_reason = reason
        self.ended_at = time.time()

        # Persist whatever the in-flight exchange already has: a hangup during
        # the assistant's reply should still leave the transcript in history.
        await self._flush_exchange({})

        current = asyncio.current_task()
        if self._deadline_task is not None and self._deadline_task is not current:
            self._deadline_task.cancel()

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug(
                    "Voice: sideband close failed for %s", self.session_id,
                    exc_info=True,
                )

        if self._reader_task is not None and self._reader_task is not current:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug(
                    "Voice: sideband reader ended with an error for %s",
                    self.session_id, exc_info=True,
                )

        self.connected = False
        logger.info(
            "Voice: sideband detached for session %s (call %s, reason %s)",
            self.session_id, self.call_id, reason,
        )

    async def _duration_cap(self) -> None:
        """Close the call once it exceeds ``voice_max_call_minutes`` (VM13)."""
        try:
            await asyncio.sleep(self.max_call_minutes * SECONDS_PER_MINUTE)
        except asyncio.CancelledError:
            raise
        logger.warning(
            "Voice: session %s hit the %s-minute call cap — closing the sideband",
            self.session_id, self.max_call_minutes,
        )
        await self.stop(reason="duration_cap")

    # -- outbound ----------------------------------------------------------

    async def _send_session_update(self) -> None:
        """Send the initial ``session.update`` (instructions + turn detection).

        NOTE: the retention/truncation knob from ruling VM8 (``retention_ratio``
        0.8) is deliberately NOT sent — the exact field shape for the current
        realtime schema was not verifiable here, and a rejected ``session.update``
        would silently drop instructions AND VAD config for the whole call. The
        API's default truncation applies until a verified shape lands.
        """
        instructions = VOICE_INSTRUCTIONS_V1
        seed = await run_sync(build_context_seed, self.session_id)
        if seed:
            instructions = f"{instructions}\n\n{seed}"

        await self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "audio": {"input": {"turn_detection": dict(TURN_DETECTION_CONFIG)}},
            },
        })

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("voice sideband is not connected")
        await self._ws.send(json.dumps(payload))

    # -- inbound -----------------------------------------------------------

    async def _read_loop(self) -> None:
        """Read sideband events until the socket closes. Never auto-reconnects."""
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    logger.debug(
                        "Voice: unparsable sideband frame for %s", self.session_id,
                    )
                    continue
                if not isinstance(event, dict):
                    continue
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.warning(
                        "Voice: sideband event handling failed for %s (type=%r)",
                        self.session_id, event.get("type"), exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Voice: sideband read loop ended abnormally for %s",
                self.session_id, exc_info=True,
            )
        finally:
            # Reconnect is FE-driven with a fresh call_id (ruling VM15): all we
            # do here is stop claiming to be connected.
            self.connected = False
            logger.info(
                "Voice: sideband stream closed for session %s", self.session_id,
            )

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        etype = event.get("type") or ""

        if etype == "conversation.item.input_audio_transcription.completed":
            text = _clean(event.get("transcript"))
            if text:
                self._pending_user.append(text)
            return

        if etype == "response.output_audio_transcript.done":
            text = _clean(event.get("transcript"))
            if text:
                self._pending_assistant.append(text)
            return

        if etype == "response.done":
            response = event.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            await self._flush_exchange(usage if isinstance(usage, dict) else {})
            return

        if etype == "error":
            logger.warning(
                "Voice: sideband reported an error for %s: %s",
                self.session_id, str(event.get("error"))[:500],
            )
            return

        logger.debug(
            "Voice: ignoring sideband event type=%r for %s", etype, self.session_id,
        )

    # -- persistence -------------------------------------------------------

    async def _flush_exchange(self, usage: Dict[str, Any]) -> None:
        """Persist the assembled exchange (VM9) and reset the buffers.

        Pairing note: the user's input transcription usually lands before the
        response completes, but it is not ordered by the protocol. When it has
        not arrived, the exchange persists assistant-only (``user_text=None``) —
        the same tolerance a model-initiated response needs.
        """
        user_text = " ".join(self._pending_user).strip() or None
        assistant_text = " ".join(self._pending_assistant).strip() or None
        self._pending_user = []
        self._pending_assistant = []
        if not user_text and not assistant_text:
            return

        # A voice session never runs a real (turns-table) turn in V1, so writing
        # unfenced events is safe by construction. Cheap belt-and-braces: if a
        # live turn somehow exists, skip the write rather than interleave with a
        # fenced recorder.
        try:
            from app.db import turns as turns_db

            live = await run_sync(turns_db.get_live_turn, self.session_id)
        except Exception:
            live = None
        if live:
            logger.warning(
                "Voice: skipping transcript write for %s — a live turn (%s) owns "
                "the session", self.session_id, live.get("id"),
            )
            return

        from app.core import turn_recorder

        await turn_recorder.record_voice_exchange(
            self.session_id,
            _cap(user_text),
            _cap(assistant_text),
            model=self.model,
            usage={"voice_realtime": usage} if usage else {},
        )


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _cap(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return text[:TRANSCRIPT_MAX_CHARS]


# ============================================================================
# Registry — one live bridge per session
# ============================================================================

class VoiceCallRegistry:
    """In-process map of ``session_id -> VoiceBridge`` (ruling VM8)."""

    def __init__(self) -> None:
        self._bridges: Dict[str, VoiceBridge] = {}
        self._lock = asyncio.Lock()

    def get(self, session_id: str) -> Optional[VoiceBridge]:
        return self._bridges.get(session_id)

    def state(self, session_id: str) -> Dict[str, Any]:
        """State payload for a session, live bridge or not."""
        bridge = self._bridges.get(session_id)
        if bridge is None:
            return {
                "active": False, "connected": False,
                "started_at": None, "model": None,
            }
        return bridge.state()

    async def attach(
        self,
        session_id: str,
        call_id: str,
        *,
        api_key: str,
        model: str,
        max_call_minutes: int,
    ) -> VoiceBridge:
        """Attach the sideband, REPLACING any existing attachment.

        Replacement (rather than rejection) is what makes the FE's reconnect
        near OpenAI's session wall work: the FE re-handshakes with a fresh
        ``call_id`` and posts it here, the old attachment is closed, and the new
        one reseeds context from the persisted transcript.

        The lock is held across the connect so two concurrent posts cannot both
        end up registered.
        """
        async with self._lock:
            previous = self._bridges.pop(session_id, None)
            if previous is not None:
                logger.info(
                    "Voice: replacing sideband for session %s (call %s -> %s)",
                    session_id, previous.call_id, call_id,
                )
                await previous.stop(reason="replaced")

            bridge = VoiceBridge(
                session_id, call_id,
                api_key=api_key, model=model, max_call_minutes=max_call_minutes,
            )
            await bridge.start()
            self._bridges[session_id] = bridge
            return bridge

    async def detach(self, session_id: str) -> bool:
        """Stop and drop the session's bridge. ``False`` when there was none."""
        async with self._lock:
            bridge = self._bridges.pop(session_id, None)
        if bridge is None:
            return False
        await bridge.stop(reason="hangup")
        return True

    async def shutdown_all(self) -> None:
        """Close every live bridge (app shutdown). Never raises."""
        async with self._lock:
            bridges = list(self._bridges.values())
            self._bridges.clear()
        for bridge in bridges:
            try:
                await bridge.stop(reason="shutdown")
            except Exception:
                logger.warning(
                    "Voice: bridge shutdown failed for %s", bridge.session_id,
                    exc_info=True,
                )


registry = VoiceCallRegistry()


async def shutdown_all() -> None:
    """Module-level lifespan hook (see ``app/main.py``)."""
    await registry.shutdown_all()
