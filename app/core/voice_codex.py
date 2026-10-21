"""Unofficial, unsupported ChatGPT subscription voice over Codex WebRTC.

Reuses the server-owned PCM browser leg and same-chat delegation lifecycle.
Explicit engine selection opts in; the default remains API-key GPT Live.
"""
import asyncio
import json

from app.core.voice_stream import VoiceStream
from app.core.voice_codex_transport import CodexPeer, codex_session
from app.db.async_database import run_sync


class CodexVoiceStream(VoiceStream):
    @property
    def is_live(self):
        return True

    async def open_upstream(self):
        return CodexPeer()

    def reconnect_reason(self, reader, done):
        # Never silently retry a subscription call or use the API-key socket.
        return None

    async def start_live(self, history):
        from app.core.http_client import get_http_client
        from app.core.provider.fluid_credentials import _load_codex_access_token
        from app.core.voice_bridge import conversation_instructions
        from app.db import database
        options = dict(self.options)
        session = codex_session(self.config, options, history)
        if self.config.get("conversation_surface"):
            row = await run_sync(database.get_session, self.session_id)
            session["instructions"] += "\n\n" + await run_sync(conversation_instructions, row)
        token, account = await _load_codex_access_token(self.config["provider_id"])
        try:
            async with asyncio.timeout(20):
                await self.upstream.open(get_http_client(), token, account, session)
                pending = []
                while True:
                    event = json.loads(await self.upstream.recv())
                    if event.get("type") == "error":
                        raise ValueError("Unofficial ChatGPT voice access was refused.")
                    if event.get("type") == "session.started":
                        break
                    pending.append(event)
                    if len(pending) > 128:
                        raise ValueError("Unofficial ChatGPT voice did not start.")
                for event in pending:
                    self.upstream.enqueue(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # HTTP/network exceptions may carry request headers. Never pass them
            # into the shared lifecycle's logging/browser error channel.
            raise ValueError("Unofficial ChatGPT voice could not start; access may be unavailable.") from None
        finally:
            self.api_key = ""
        if self.options["opening"] and not history and self.config.get("greet", True):
            await self.send({"type": "session.instructions.append", "content":
                "Greet immediately, then listen: " + self.options["opening"]})
