"""Server-owned audio transports for GPT Live and fal.ai PersonaPlex.

The browser sends PCM16/24kHz mono, never provider credentials or tool events.
Delegation is dispatched by the server using the session's selected model.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

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
    payload: dict = {"user_message_already_recorded": True}
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


class VoiceStream:
    def __init__(self, websocket: WebSocket, row: dict, config: dict, api_key: str, max_minutes: int):
        self.websocket = websocket
        self.session_id = row["id"]
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
        self.transcript: deque[dict] = deque(maxlen=200)
        self.pending = {"user": "", "assistant": ""}
        self.last_caption_at = 0.0
        self.caption_ids = {"user": 0, "assistant": 0}
        self.delegations: set[str] = set()
        self.delegation_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        self.last_caption_role = None
        self.progress = None
        self.transcript_seq = 0
        self.delegated_seq = 0

    @property
    def is_live(self):
        return self.config["model"] == LIVE_MODEL

    async def send(self, event: dict):
        if not self.closed and self.upstream:
            await self.upstream.send(json.dumps(event))

    async def emit(self, event: dict):
        if not self.closed:
            async with self.send_lock:
                await self.websocket.send_json(event)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.connected = False
        if self.upstream:
            await self.upstream.close()
        try:
            await self.websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def flush(self):
        from app.core.turn_recorder import record_voice_exchange
        # Checkpoint, handoff and speaker changes can flush concurrently. Keep
        # their durable turns in arrival order, including during barge-in.
        async with self.flush_lock:
            user, assistant = self.pending["user"], self.pending["assistant"]
            self.pending = {"user": "", "assistant": ""}
            if user or assistant:
                self.caption_ids["user"] += 1
                self.caption_ids["assistant"] += 1
                await record_voice_exchange(self.session_id, user or None, assistant or None, model=self.config["model"])

    async def caption(self, role: str, delta: str, offset: int = 0):
        if not delta:
            return
        # Do not combine overlapping speakers into a user-first ten-second
        # bucket: it reverses assistant speech that arrived before a barge-in.
        if self.last_caption_role is not None and role != self.last_caption_role:
            await self.flush()
        self.last_caption_role = role
        self.transcript_seq += 1
        self.transcript.append({"role": role, "text": delta, "offset_ms": offset, "seq": self.transcript_seq})
        self.pending[role] += delta
        self.last_caption_at = time.monotonic()
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
            if not any(e["role"] == "user" and e["text"].strip() for e in conversation):
                result = "The backend has not received your request. Please repeat it."
            else:
                await self.flush()
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
                    if len(_workers) >= 8:
                        await self.send({"type": "session.commentary.append", "delegation_id": did,
                                         "content": "The backend is busy with another task. Ask the user to repeat this new request after it finishes."})
                    else:
                        # Capture only speech since the previous handoff, before
                        # yielding. The chat already owns the earlier history.
                        entries = [e for e in self.transcript if e.get("seq", 0) > self.delegated_seq]
                        self.delegated_seq = self.transcript_seq
                        self.delegation_task = asyncio.create_task(self.run_delegation(did, entries))
                        _workers.add(self.delegation_task)
                        self.delegation_task.add_done_callback(_workers.discard)
                elif kind in ("error", "session.error"):
                    raise ValueError(str((event.get("error") or {}).get("message") or "The Live API rejected an event."))
                elif kind == "session.closed":
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
            if time.monotonic() - self.last_caption_at >= 2:
                await self.flush()

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
        if self.options["opening"] and not history and self.config.get("greet", True):
            # Provider rejection is surfaced by receive_upstream, not silently
            # treated as spoken audio. Never greet again after conversation replay.
            event = live_opening_event(self.options["opening"])
            self.opening_event_id = event["event_id"]
            await self.send(event)

    async def run(self):
        tasks = []
        url = "wss://api.openai.com/v1/live/sessions" if self.is_live else "wss://fal.run/fal-ai/personaplex/realtime"
        auth = ("Bearer " if self.is_live else "Key ") + self.api_key
        budget = asyncio.timeout(self.max_seconds)
        try:
            async with budget:
                async with connect(url, additional_headers={"Authorization": auth}, open_timeout=45, max_size=2_000_000) as upstream:
                    self.upstream = upstream
                    self.api_key = ""
                    if self.closed:
                        return
                    if self.is_live:
                        from app.core.voice_bridge import build_context_seed
                        seed = await run_sync(build_context_seed, self.session_id)
                        history = ([{"type": "message", "role": "user", "content": [{"type": "input_text", "text": seed}]}] if seed else [])
                        if seed:
                            self.transcript.append({"role": "history", "text": seed, "offset_ms": 0})
                        if self.options["delegation_type"] != "responses" or self.config.get("conversation_surface"):
                            from app.core.voice_progress import VoiceProgress
                            self.progress = VoiceProgress(self.session_id, self.send)
                            await self.progress.open()
                        await self.start_live(history)
                    else:
                        await self.prime_fal()
                    self.connected = True
                    if not self.config.get("conversation_surface"):
                        await run_sync(database.update_session, self.session_id, launched_model=self.config["model"])
                    await run_sync(database.set_session_active, self.session_id, True)
                    await self.emit({"type": "ready", "model": self.config["model"], "sample_rate": 24000})
                    tasks = [asyncio.create_task(self.receive_browser()), asyncio.create_task(self.receive_upstream()), asyncio.create_task(self.checkpoint())]
                    if self.progress is not None:
                        tasks.append(asyncio.create_task(self.progress.run()))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
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
            logger.warning("Voice audio stream failed for %s: %s", self.session_id, type(exc).__name__)
            try:
                await self.emit({"type": "error", "message": str(exc)[:300]})
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.progress is not None:
                self.progress.close()
            await self.close()
            await self.flush()
            if calls.get(self.session_id) is self:
                calls.pop(self.session_id, None)
                if not self.config.get("conversation_surface"):
                    await run_sync(database.set_session_active, self.session_id, False)


async def stop(session_id: str) -> bool:
    call = calls.get(session_id)
    if call:
        await call.close()
    return bool(call)


def state(session_id: str) -> dict | None:
    call = calls.get(session_id)
    if not call:
        return None
    return {"active": not call.closed, "connected": call.connected, "started_at": call.started_at,
            "model": call.config["model"], "active_tasks": call.progress.active_count if call.progress is not None else int(bool(call.delegation_task and not call.delegation_task.done()))}
