"""ChatGPT subscription voice (GPT-Live-1.1) over Codex WebRTC.

Reuses the server-owned PCM browser leg and same-chat delegation lifecycle.
Explicit engine selection opts in; the default remains API-key GPT Live.
"""
import asyncio
import json
import time

from app.core.voice_stream import RENEW_LEAD_S, VoiceReconnectRefused, VoiceStream, renew_subscription_login
from app.core.voice_codex_transport import CodexCallRefused, CodexPeer, CodexPeerClosed, codex_session
from app.db.async_database import run_sync


# No duration/expiry is specified by openai/codex 430a16799d90e296fb27b1e94f206023902b7e56:
# realtime_websocket/protocol_frameless_bidi.rs accepts session.started/updated;
# protocol_common.rs reads only id/instructions. In the absence of a reported
# expiry, proactively renew around 50 minutes at a quiet moment (owner policy,
# not a claimed ChatGPT session lifetime). The shared renewal window needs a
# soft deadline RENEW_LEAD_S later than that target.
CODEX_RENEW_S = 50 * 60
# Reasonless/transport closes look like expiry only near that age. Explicit
# "expired" closes instead use the shared minimum session age (60 seconds).
CODEX_EXPIRY_CLOSE_S = 50 * 60


class CodexVoiceStream(VoiceStream):
    provider_label = "ChatGPT voice"
    close_event = None  # The WebRTC peer close ends the old call.
    START_FAILED = "ChatGPT voice could not start. Check your ChatGPT login in Settings > Providers, then try again."

    @property
    def is_live(self):
        return True

    async def connect_provider(self):
        # Every renewal gets fresh media, event queues and subscription credentials
        # (loaded by start_live), never VoiceStream's API-key WebSocket.
        return CodexPeer()

    async def open_upstream(self):
        return await self.connect_provider()

    def reconnect_reason(self, reader, done):
        if self.closed or reader is None:
            return None
        if reader not in done:
            if self.expires_at and not self.deaf.is_set():
                return "renew"
        elif reader.cancelled():
            return None
        elif reader.exception() is None:
            if self.provider_close_reason == "expired" and self.is_renewal("expired"):
                return "expired"
            if self.provider_close_reason in ("closed", "connection_lost") and (
                    time.monotonic() - self.session_started_at >= CODEX_EXPIRY_CLOSE_S):
                return "expired"
        # Early closes, rejected events and arbitrary failures are not evidence
        # of expiry. Record a failure, but do not automatically retry them.
        self.count_failure()
        return None

    def quiet_for_renewal(self):
        return super().quiet_for_renewal() and self.is_quiet()

    async def send(self, event):
        try:
            await super().send(event)
        except CodexPeerClosed:
            self.hold(event)  # The reader observes the same end; never lose a note.

    async def start_live(self, history):
        if self.closed:
            self.api_key = ""
            raise ValueError("ChatGPT voice connection is closed.")
        from app.core.voice_dependencies import availability
        available, reason = availability()
        if not available:
            raise ValueError(reason)
        from app.core.http_client import get_http_client
        from app.core.provider.fluid_credentials import _load_codex_access_token
        from app.core.voice_bridge import conversation_instructions
        from app.db import database
        options = dict(self.options)
        session = codex_session(self.config, options, history)
        if self.config.get("conversation_surface"):
            row = await run_sync(database.get_session, self.session_id)
            session["instructions"] += "\n\n" + await run_sync(conversation_instructions, row)
        self.expires_at = None
        self.opening_event_id = None
        self.heard_this_session = False
        self.unheard_voiced_s = 0.0
        self.deaf.clear()
        try:
            token, account = await _load_codex_access_token(self.config["provider_id"])
            if self.closed:
                raise ValueError("ChatGPT voice connection is closed.")
            async with asyncio.timeout(20):
                await self.upstream.open(get_http_client(), token, account, session)
                pending = []
                while True:
                    event = json.loads(await self.upstream.recv())
                    if event.get("type") == "error":
                        raise ValueError("ChatGPT voice access was refused.")
                    expiry = (event.get("session") or {}).get("expires_at")
                    if expiry is not None:
                        self.expires_at = expiry  # Sanitized by incoming_event in the adapter.
                    if event.get("type") == "session.started":
                        break
                    if event.get("type") == "session.closed":
                        raise ValueError("ChatGPT voice ended before starting.")
                    pending.append(event)
                    if len(pending) > 128:
                        raise ValueError("ChatGPT voice did not start.")
                for event in pending:
                    self.upstream.enqueue(event)
        except asyncio.CancelledError:
            raise
        except CodexCallRefused as exc:
            if exc.status == 401:
                if await self.renew_after_401(token):
                    raise ValueError("The ChatGPT login was renewed. Start the call again.") from None
                raise VoiceReconnectRefused(
                    "ChatGPT refused the login. Reconnect ChatGPT in Settings > Providers; there is no API-key fallback.") from None
            if exc.status in (403, 429):
                raise VoiceReconnectRefused(self.START_FAILED) from None
            raise ValueError(self.START_FAILED) from None
        except Exception:
            # HTTP/network exceptions may carry request headers. Never pass them
            # into the shared lifecycle's logging/browser error channel.
            raise ValueError(self.START_FAILED) from None
        finally:
            self.api_key = ""
        self.session_started_at = time.monotonic()
        if self.expires_at is None:
            self.expires_at = time.time() + CODEX_RENEW_S + RENEW_LEAD_S
        if self.restoring:
            return  # History includes the resume note; never greet again.
        note = await self.greeting(history)
        if note:
            await self.send({"type": "session.instructions.append", "content": note})

    async def renew_after_401(self, token) -> bool:
        from app.core import codex_token_refresh
        return await renew_subscription_login(
            codex_token_refresh.refresh_on_401_once, self.config["provider_id"], token,
            label="ChatGPT", session_id=self.session_id)
