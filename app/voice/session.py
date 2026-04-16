"""
Realtime voice — VoiceSession lifecycle.

A VoiceSession encapsulates one active voice call: WebRTC peer connection,
Pipecat pipeline task, background runner, and accumulated transcript/metrics.

The VoiceSessionManager tracks live sessions process-wide for enforcement
of concurrency caps, resource monitoring, and clean shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.voice.pipeline import PipelineComponents

logger = logging.getLogger(__name__)


@dataclass
class TranscriptEntry:
    """Single transcript line (final) kept for persistence."""
    text: str
    timestamp: float
    role: str = "user"


@dataclass
class VoiceSession:
    """
    A single in-flight realtime voice call.

    Created by /voice/realtime/offer, torn down when the client hangs up
    or the pipeline exits.
    """
    id: str
    user_id: Optional[str]
    project_id: Optional[str]
    session_id: Optional[str]                 # AI Shuffle chat session id (Phase 2)
    worktree_path: Optional[str] = None       # for Phase 2 cwd binding
    transport_handle: Any = None              # app.voice.transport.TransportHandle
    components: Optional[PipelineComponents] = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    task_handle: Optional[asyncio.Task] = None
    closed: bool = False
    close_reason: Optional[str] = None
    # Usage / cost tracking (aggregated over the call's lifetime).
    stt_provider: str = ""
    stt_model: str = ""
    tts_provider: str = ""
    tts_model: str = ""
    llm_model: str = ""
    stt_seconds: float = 0.0
    tts_chars: int = 0
    profile_id: Optional[str] = None

    async def run(self) -> None:
        """Run the Pipecat task until disconnect. Must be awaited in background."""
        if self.components is None:
            raise RuntimeError("VoiceSession has no pipeline components")
        try:
            await self.components.runner.run(self.components.task)
        except asyncio.CancelledError:
            self.close_reason = self.close_reason or "cancelled"
            raise
        except Exception as e:
            logger.exception("Voice session %s pipeline error: %s", self.id, e)
            self.close_reason = self.close_reason or f"error: {e}"
        finally:
            self.closed = True

    async def close(self, reason: str = "client_disconnect") -> None:
        """Tear down pipeline and peer connection. Idempotent. Logs usage."""
        if self.closed:
            return
        self.close_reason = reason
        try:
            if self.components and self.components.task:
                await self.components.task.cancel()
        except Exception:
            logger.debug("VoiceSession.close: task.cancel failed", exc_info=True)
        try:
            if self.transport_handle and hasattr(self.transport_handle, "peer_connection"):
                pc = self.transport_handle.peer_connection
                if hasattr(pc, "close"):
                    await pc.close()
        except Exception:
            logger.debug("VoiceSession.close: peer_connection.close failed", exc_info=True)

        # Persist usage to the existing usage_log table (reuses text-chat schema).
        # Best-effort: swallow failures so teardown stays resilient.
        try:
            self._persist_usage()
        except Exception:
            logger.debug("VoiceSession.close: persist_usage failed", exc_info=True)

        self.closed = True

    def _persist_usage(self) -> None:
        """Record aggregated voice usage into usage_log."""
        from app.db import analytics as db_analytics
        from app.voice.pricing import stt_cost_usd, tts_cost_usd

        # Estimate STT seconds from call duration if not updated incrementally
        # (current pipeline doesn't tally seconds per chunk — rough proxy).
        if self.stt_seconds == 0.0:
            self.stt_seconds = max(0.0, time.time() - self.created_at)

        stt_cost = stt_cost_usd(self.stt_provider, self.stt_model, self.stt_seconds)
        tts_cost = tts_cost_usd(self.tts_provider, self.tts_model, self.tts_chars)
        total_cost = round(stt_cost + tts_cost, 6)

        if total_cost <= 0 and self.stt_seconds < 1:
            return  # nothing meaningful to log

        duration_ms = int((time.time() - self.created_at) * 1000)
        model_tag = (
            f"voice:{self.stt_provider}+{self.tts_provider}"
            if self.stt_provider or self.tts_provider
            else "voice"
        )

        db_analytics.log_usage(
            session_id=self.session_id,
            profile_id=self.profile_id,
            model=model_tag,
            tokens_in=0,
            tokens_out=self.tts_chars,  # rough proxy for output volume
            cost_usd=total_cost,
            duration_ms=duration_ms,
        )

    def touch(self) -> None:
        self.last_activity = time.time()

    def add_transcript(self, text: str, role: str = "user") -> None:
        self.transcript.append(TranscriptEntry(text=text, timestamp=time.time(), role=role))
        self.touch()


class VoiceSessionManager:
    """
    Process-wide registry of live VoiceSessions.

    Enforces a soft concurrency cap and exposes lookup by id so the
    voice API can signal interruptions, fetch transcripts, or tear
    down on user action.
    """

    def __init__(self, max_concurrent: int = 8):
        self._sessions: dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent

    async def register(self, session: VoiceSession) -> None:
        async with self._lock:
            if len(self._sessions) >= self._max_concurrent:
                raise RuntimeError(
                    f"Voice session limit reached ({self._max_concurrent}). "
                    "Wait for an active call to end."
                )
            self._sessions[session.id] = session

    async def unregister(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    def get(self, session_id: str) -> Optional[VoiceSession]:
        return self._sessions.get(session_id)

    def all(self) -> list[VoiceSession]:
        return list(self._sessions.values())

    async def close_all(self, reason: str = "shutdown") -> None:
        sessions = list(self._sessions.values())
        await asyncio.gather(
            *(s.close(reason=reason) for s in sessions),
            return_exceptions=True,
        )
        self._sessions.clear()

    @staticmethod
    def new_id() -> str:
        return f"voice_{uuid.uuid4().hex[:16]}"


# Module-level singleton. Imported by the FastAPI endpoint.
voice_session_manager = VoiceSessionManager()
