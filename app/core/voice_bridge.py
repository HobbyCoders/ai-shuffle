"""Server-side sideband bridge for voice sessions (waves VW3 + VW4, ruling VM8).

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

VW4 adds the delegation layer (rulings VM10/VM11/VM12/VM16): four function
tools declared on the sideband (``dispatch_agent``, ``check_agent``,
``list_agents``, ``respond_approval``), the servicing loop that answers them
with ``function_call_output`` + ``response.create``, a per-worker WATCHER that
injects "the agent finished" back into the conversation unprompted, and an
approval poller that reads pending worker permission prompts out loud.

Deliberately NOT here (scope fences):

* No server-side reconnect. When the sideband drops, the bridge simply marks
  itself disconnected — reconnect is FE-driven with a fresh ``call_id``, and
  the replace-attach path reseeds context (see :func:`build_context_seed`).
* No worker teardown. Dispatched agents SURVIVE hangup (ruling VM12): ``stop``
  cancels only the bridge's own tasks — it must never call ``teardown_workers``
  or any other worker-stopping API.
* No direct-work tools for the voice model, ever (ruling VM16). The tool
  surface is exactly the four dispatch/telemetry calls above.

Robustness rule (mirrors the recorder): a bridge bug must never take down the
call or the request that started it — the read loop swallows and logs per-event
failures, tool servicing runs in its own task and turns any exception into an
error payload, and persistence failures never propagate.
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

# The delegation-first coordinator contract (ruling VM16, user-dictated text —
# do not paraphrase it).
VOICE_INSTRUCTIONS = (
    "You are the ai-shuffle voice agent — the user's coding coworker. You are a "
    "COORDINATOR only: you never do substantive work yourself. For anything "
    "beyond a trivial single-tool-call answer (a status check, listing agents), "
    "dispatch the work to a worker agent with dispatch_agent and keep the "
    "conversation going. Acknowledge the ask in one short sentence, dispatch, "
    "then tell the user you've got an agent on it. When a completion update "
    "arrives, tell the user what landed and ask if there's anything else they'd "
    "like. Keep every reply short, natural and spoken-style — no markdown, no "
    "lists, no code read-outs. If a worker asks for permission to use a tool, "
    "read the request to the user in one short sentence and act only on their "
    "explicit spoken yes or no, via respond_approval. Never invent results; if "
    "you don't know, use check_agent or say you don't know."
)

#: Back-compat alias — VW3 shipped the placeholder under this name.
VOICE_INSTRUCTIONS_V1 = VOICE_INSTRUCTIONS

# The whole tool surface (ruling VM10). Four calls, none of which does work
# directly: three read/dispatch against the existing Task-dispatch layer and one
# answers a worker's permission prompt. Realtime function-tool schema is flat —
# ``{"type": "function", "name", "description", "parameters"}`` — NOT the
# Chat-Completions ``{"type": "function", "function": {...}}`` nesting.
VOICE_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "dispatch_agent",
        "description": (
            "Spin up an ai-shuffle worker agent to actually do a piece of work "
            "(write code, investigate, run commands). Returns IMMEDIATELY with a "
            "task id — the worker keeps running in the background and you will "
            "be told when it finishes, so never wait or poll for it. Tell the "
            "user you have an agent on it and carry on the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The full task for the worker, in text. Include "
                        "everything it needs — it cannot hear the conversation."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Optional agent name or id from list_agents. Omit to use "
                        "the default worker agent."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Optional short label for this task, e.g. 'dark mode "
                        "toggle' — used when reporting back to the user."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "type": "function",
        "name": "check_agent",
        "description": (
            "Check on a worker agent you dispatched: its current status and, "
            "once it has finished, a bounded report of what it did. Use this "
            "when the user asks how something is going."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task id returned by dispatch_agent.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "type": "function",
        "name": "list_agents",
        "description": (
            "List the agent definitions available to dispatch and the tasks "
            "currently active in this session."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "respond_approval",
        "description": (
            "Answer a worker agent's pending permission request. Only ever call "
            "this after the user has explicitly said yes or no out loud."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The request id from the approval prompt.",
                },
                "decision": {
                    "type": "string",
                    "enum": ["allow", "deny"],
                    "description": "What the user said: allow or deny.",
                },
            },
            "required": ["request_id", "decision"],
        },
    },
]

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

# Role used for the items the BACKEND injects mid-conversation (worker
# completions, approval prompts). ``user`` — not ``system`` — deliberately:
# a "user" message item with ``input_text`` content is the one shape every
# realtime revision accepts, and an item the API rejects would silently kill
# the whole completion-injection mechanism (the thing that makes "I'll let you
# know when it lands" real). The ``[agent update]`` / ``[approval needed]``
# prefixes plus the instruction contract are what keep the model from reading
# these out as if the user had spoken them.
INJECTED_ITEM_ROLE = "user"

# Prefixes for injected items — also what the instructions refer to.
COMPLETION_PREFIX = "[agent update]"
APPROVAL_PREFIX = "[approval needed]"

# Bounds on injected text so a chatty worker can never blow up the realtime
# context (the model only has to SAY the gist; the full report lives in the
# session feed).
INJECTED_REPORT_MAX_CHARS = 1200

# Hard ceiling on how long a completion watcher waits for its worker, whatever
# the call-duration setting says. A worker outliving this just stops being
# announced — it keeps running (ruling VM12) and stays readable via check_agent.
WATCHER_MAX_SECONDS = 3600.0

# How often the bridge re-reads pending worker permission prompts.
APPROVAL_POLL_SECONDS = 2.0

# Bounds on the list_agents payload — this is spoken aloud, not read.
MAX_LISTED_AGENTS = 40
MAX_LISTED_TASKS = 20
AGENT_DESCRIPTION_MAX_CHARS = 160


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

        # -- VW4 tool servicing ------------------------------------------
        # Function calls already serviced, by call_id: the same call reaches us
        # twice (once as response.function_call_arguments.done, once inside
        # response.done), and servicing it twice would dispatch two workers.
        self._serviced_calls: set = set()
        self._call_tasks: set = set()
        # One watcher per dispatched worker (announces its completion).
        self._watchers: set = set()
        self._approval_task: Optional[asyncio.Task] = None
        self._announced_approvals: set = set()
        # Voice session's profile id, read lazily and cached (dispatch grants).
        self._profile_id: Optional[str] = None
        self._profile_loaded = False

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

        # Cancel only the bridge's OWN tasks. Dispatched workers deliberately
        # survive the call (ruling VM12) — nothing here may stop them, and this
        # module never calls ``teardown_workers``/``stop_all_workers``.
        for task in list(self._call_tasks) + list(self._watchers):
            if task is not current:
                task.cancel()
        self._call_tasks.clear()
        self._watchers.clear()
        if self._approval_task is not None and self._approval_task is not current:
            self._approval_task.cancel()
        self._approval_task = None

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
        """Send the initial ``session.update`` (instructions, tools, turn detection).

        NOTE: the retention/truncation knob from ruling VM8 (``retention_ratio``
        0.8) is deliberately NOT sent — the exact field shape for the current
        realtime schema was not verifiable here, and a rejected ``session.update``
        would silently drop instructions AND VAD config for the whole call. The
        API's default truncation applies until a verified shape lands.
        """
        instructions = VOICE_INSTRUCTIONS
        seed = await run_sync(build_context_seed, self.session_id)
        if seed:
            instructions = f"{instructions}\n\n{seed}"

        await self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                # Declared HERE, on the sideband, not by the browser: the client
                # can neither see nor tamper with the dispatch surface (VM2).
                "tools": [dict(tool) for tool in VOICE_TOOLS],
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

        if etype == "response.function_call_arguments.done":
            # The early signal — arguments are complete before the response is.
            # Some revisions omit ``name`` here; those calls are picked up from
            # the response.done output item instead (same call_id, deduped).
            self._maybe_service_call(
                event.get("call_id"), event.get("name"), event.get("arguments"),
            )
            return

        if etype == "response.done":
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            for item in response.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    self._maybe_service_call(
                        item.get("call_id"), item.get("name"), item.get("arguments"),
                    )
            usage = response.get("usage")
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

    # -- tool servicing (VW4) ----------------------------------------------

    def _maybe_service_call(
        self, call_id: Any, name: Any, arguments: Any
    ) -> Optional[asyncio.Task]:
        """Start servicing one function call, unless it was already serviced.

        Deduped by ``call_id`` because the SAME call reaches the bridge twice
        (``response.function_call_arguments.done`` first, then as an output item
        of ``response.done``) — dispatching a worker twice for one ask is the
        failure this set exists to prevent.

        Servicing runs in its own task: a dispatch takes seconds (worker
        registration), and the read loop must stay free to keep reading — the
        realtime model goes on talking while the call is outstanding.
        """
        if not isinstance(call_id, str) or not call_id:
            return None
        if not isinstance(name, str) or not name:
            return None
        if call_id in self._serviced_calls:
            return None
        if not self.active:
            return None
        self._serviced_calls.add(call_id)

        task = asyncio.create_task(
            self._service_call(call_id, name, arguments),
            name=f"voice-tool-{name}-{self.session_id}",
        )
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)
        return task

    async def _service_call(self, call_id: str, name: str, arguments: Any) -> None:
        """Run one tool call and send its result back onto the call."""
        args = _parse_arguments(arguments)
        try:
            if name == "dispatch_agent":
                result = await self._tool_dispatch_agent(args)
            elif name == "check_agent":
                result = await self._tool_check_agent(args)
            elif name == "list_agents":
                result = await self._tool_list_agents()
            elif name == "respond_approval":
                result = await self._tool_respond_approval(args)
            else:
                result = {"ok": False, "error": f"Unknown tool '{name}'."}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a tool bug must not end the call
            logger.warning(
                "Voice: tool %s failed for session %s", name, self.session_id,
                exc_info=True,
            )
            result = {"ok": False, "error": f"{name} failed: {exc}"}

        await self._send_call_output(call_id, result)

    async def _send_call_output(self, call_id: str, result: Dict[str, Any]) -> None:
        """Hand a tool result back and ask the model to speak the outcome."""
        if not self.active:
            return
        try:
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, default=str),
                },
            })
            await self._send({"type": "response.create"})
        except Exception:  # noqa: BLE001
            logger.warning(
                "Voice: could not deliver tool result for %s (call %s)",
                self.session_id, call_id, exc_info=True,
            )

    async def _caller_profile_id(self) -> Optional[str]:
        """The voice session's ``profile_id`` — the dispatch grant identity."""
        if self._profile_loaded:
            return self._profile_id
        try:
            row = await run_sync(database.get_session, self.session_id)
            self._profile_id = (row or {}).get("profile_id")
        except Exception:  # noqa: BLE001 — dispatch falls back to the row read
            logger.debug(
                "Voice: could not read profile for %s", self.session_id, exc_info=True,
            )
            self._profile_id = None
        self._profile_loaded = True
        return self._profile_id

    async def _tool_dispatch_agent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Spin up a background worker and ACK immediately (ruling VM10).

        The ack is the tool result — the model says "I've got an agent on it"
        and keeps talking. The completion arrives later, injected by the watcher
        this starts, which is what makes "I'll let you know when it lands" real.
        """
        from app.core import task_dispatch

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "dispatch_agent needs a prompt."}

        label = (args.get("label") or "").strip() or None
        agent = (args.get("agent") or "").strip()
        if not agent:
            agent = await self._default_agent_ref() or ""

        payload = await task_dispatch.dispatch_task_tool(
            {
                "prompt": prompt,
                "agent": agent,
                "label": label,
                "mode": "background",
            },
            caller_session_id=self.session_id,
            caller_profile_id=await self._caller_profile_id(),
        )
        # dispatch_task_tool never raises; ok=False carries an actionable error
        # (unknown agent, depth/budget/cycle refusal) that the model reads out.
        if not payload.get("ok"):
            return dict(payload)

        worker_id = payload.get("worker_session_id")
        result = {
            "ok": True,
            "task_id": worker_id,
            "agent": payload.get("agent"),
            "label": label,
            "note": (
                "Dispatched. The worker is running in the background — you will "
                "be told when it finishes; do not poll."
            ),
        }
        if worker_id:
            self._start_watcher(str(worker_id), label)
            self._ensure_approval_poller()
        else:
            # Registration timed out inside dispatch_task_tool: the worker IS
            # running, we just have no id to watch or report against.
            result["note"] = (
                "Dispatched, but the task id did not come back in time — the "
                "worker is running; check the session feed."
            )
            self._ensure_approval_poller()
        return result

    async def _default_agent_ref(self) -> Optional[str]:
        """Agent to use when the model omits ``agent``.

        The first agent definition that is not the voice session's own profile
        (dispatching the caller's own agent is refused as a cycle by
        ``task_dispatch``). Returns None when there is nothing to pick, and the
        dispatch then fails with task_dispatch's own "needs a target" error.
        """
        try:
            rows = await run_sync(database.get_all_profiles)
        except Exception:  # noqa: BLE001
            logger.debug("Voice: agent listing failed", exc_info=True)
            return None
        caller = await self._caller_profile_id()
        for row in rows or []:
            if row.get("id") and row["id"] != caller:
                return row["id"]
        return None

    async def _tool_check_agent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Status + bounded report for a dispatched worker.

        Same read as ``TaskOutput``'s ``kind == "agent"`` branch (registry
        record + the worker's own terminal turn events), re-expressed here so the
        bridge does not depend on the MCP tool surface.
        """
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "check_agent needs a task_id."}
        return await run_sync(_agent_task_status, self.session_id, task_id)

    async def _tool_list_agents(self) -> Dict[str, Any]:
        """Available agent definitions + this session's active tasks."""
        return await run_sync(_list_agents_payload, self.session_id)

    async def _tool_respond_approval(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Answer a worker's permission prompt on the user's spoken decision.

        ``session_id`` is the VOICE session: worker approvals are redirected to
        the ROOT session (the one with a human watching), so the pending rows are
        parent-keyed. Passing the worker's id would find nothing.
        """
        from app.core.permission_handler import permission_handler

        request_id = (args.get("request_id") or "").strip()
        decision = (args.get("decision") or "").strip().lower()
        if not request_id:
            return {"ok": False, "error": "respond_approval needs a request_id."}
        if decision not in ("allow", "deny"):
            return {
                "ok": False,
                "error": f"decision must be 'allow' or 'deny' (got {decision!r}).",
            }

        result = await permission_handler.respond(
            request_id=request_id,
            session_id=self.session_id,
            decision=decision,
            broadcast_func=None,
        )
        self._announced_approvals.add(request_id)
        return {"ok": True, "request_id": request_id, "decision": decision,
                "result": result}

    # -- completion injection + approval surfacing (VW4) --------------------

    def _watch_timeout(self) -> float:
        """How long a watcher waits for its worker before giving up."""
        window = max(int(self.max_call_minutes or 0), 1) * SECONDS_PER_MINUTE
        return min(float(window), WATCHER_MAX_SECONDS)

    def _start_watcher(self, worker_session_id: str, label: Optional[str]) -> None:
        task = asyncio.create_task(
            self._watch_worker(worker_session_id, label),
            name=f"voice-watch-{worker_session_id}",
        )
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch_worker(
        self, worker_session_id: str, label: Optional[str]
    ) -> None:
        """Wait for one worker to settle, then tell the model to announce it."""
        from app.core import turn_manager

        try:
            settled = await turn_manager.wait_session_idle(
                worker_session_id, timeout=self._watch_timeout(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "Voice: worker watch failed for %s", worker_session_id, exc_info=True,
            )
            return

        # A stopped bridge means the user hung up: the worker keeps going
        # (VM12) and its result stays in the session feed — there is simply
        # nobody on the call to tell.
        if not self.active:
            return
        if not settled:
            logger.info(
                "Voice: worker %s outlived the announce window for %s",
                worker_session_id, self.session_id,
            )
            return

        status, report = await run_sync(_worker_summary, worker_session_id)
        name = label or worker_session_id
        text = f"{COMPLETION_PREFIX} Worker '{name}' finished ({status})."
        if report:
            text = f"{text} Report: {report}"
        text = f"{text} Tell the user."
        await self._inject(text)

    def _ensure_approval_poller(self) -> None:
        """Start the pending-approval poller once there is work to approve."""
        if self._approval_task is not None or not self.active:
            return
        self._approval_task = asyncio.create_task(
            self._poll_approvals(), name=f"voice-approvals-{self.session_id}",
        )

    async def _poll_approvals(self) -> None:
        """Read out each pending worker permission request exactly once.

        The GUI modal fan-out happens elsewhere and is untouched — this is the
        second, spoken leg of ruling VM11. Nothing here answers a prompt: only
        ``respond_approval``, on the user's explicit spoken decision, does.
        """
        from app.core.permission_handler import permission_handler

        while self.active:
            try:
                pending = await run_sync(
                    permission_handler.get_pending_requests, self.session_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Voice: pending-approval read failed for %s", self.session_id,
                    exc_info=True,
                )
                pending = []

            for request in pending or []:
                request_id = (request or {}).get("request_id")
                if not request_id or request_id in self._announced_approvals:
                    continue
                self._announced_approvals.add(request_id)
                await self._inject(
                    f"{APPROVAL_PREFIX} {_approval_summary(request)} — request id "
                    f"{request_id}. Ask the user whether to allow."
                )

            await asyncio.sleep(APPROVAL_POLL_SECONDS)

    async def _inject(self, text: str) -> None:
        """Put a backend-authored note into the call and ask for a reply."""
        if not self.active:
            return
        try:
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": INJECTED_ITEM_ROLE,
                    "content": [{"type": "input_text", "text": text}],
                },
            })
            await self._send({"type": "response.create"})
        except Exception:  # noqa: BLE001
            logger.warning(
                "Voice: could not inject an update into %s", self.session_id,
                exc_info=True,
            )


# ============================================================================
# Tool helpers (synchronous — called via ``run_sync``)
# ============================================================================

def _parse_arguments(arguments: Any) -> Dict[str, Any]:
    """The realtime protocol ships tool arguments as a JSON *string*."""
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _agent_task_status(session_id: str, task_id: str) -> Dict[str, Any]:
    """Registry record + terminal outcome for one dispatched worker."""
    from app.core.task_registry import task_registry

    record = task_registry.get_task(session_id, task_id)
    if record is None:
        return {
            "ok": False,
            "error": f"No task '{task_id}' in this session.",
            "task_id": task_id,
        }

    payload: Dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "label": record.get("subject"),
        "status": record.get("status"),
        "summary": record.get("summary"),
    }
    if record.get("kind") != "agent":
        payload["note"] = "Not an agent task — no report is available here."
        return payload

    try:
        from app.core import provider_agent as pa

        outcome = pa.final_turn_outcome(task_id)
        if outcome.get("terminal_found"):
            payload["worker_status"] = (
                "interrupted" if outcome.get("interrupted")
                else ("error" if outcome.get("error") else "completed")
            )
            if outcome.get("error"):
                payload["worker_error"] = outcome["error"]
            report = pa.final_assistant_text(task_id)
            if report:
                payload["report"], payload["truncated"] = pa.bound_report(report)
        else:
            payload["note"] = "Still working — no report yet."
    except Exception:  # noqa: BLE001
        payload["note"] = "Report unavailable; the work is in the session feed."
    return payload


def _worker_summary(worker_session_id: str) -> tuple:
    """``(status, bounded report tail)`` for a settled worker."""
    from app.core import provider_agent as pa

    status = "completed"
    report = ""
    try:
        outcome = pa.final_turn_outcome(worker_session_id)
        if outcome.get("terminal_found"):
            if outcome.get("interrupted"):
                status = "interrupted"
            elif outcome.get("error"):
                status = "error"
        else:
            status = "no report"
        text = pa.final_assistant_text(worker_session_id)
        if text:
            report, _ = pa.bound_report(text)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Voice: outcome read failed for worker %s", worker_session_id,
            exc_info=True,
        )
        status = "unknown"
    if len(report) > INJECTED_REPORT_MAX_CHARS:
        report = report[-INJECTED_REPORT_MAX_CHARS:].lstrip()
    return status, report


def _list_agents_payload(session_id: str) -> Dict[str, Any]:
    """Dispatchable agent definitions + the session's live tasks."""
    from app.core.task_registry import task_registry

    agents: List[Dict[str, Any]] = []
    try:
        for row in (database.get_all_profiles() or [])[:MAX_LISTED_AGENTS]:
            entry = {"id": row.get("id"), "name": row.get("name") or row.get("id")}
            description = (row.get("description") or "").strip()
            if description:
                entry["description"] = description[:AGENT_DESCRIPTION_MAX_CHARS]
            agents.append(entry)
    except Exception:  # noqa: BLE001
        logger.debug("Voice: agent listing failed", exc_info=True)

    tasks: List[Dict[str, Any]] = []
    try:
        for record in task_registry.get_all(session_id)[:MAX_LISTED_TASKS]:
            tasks.append({
                "task_id": record.get("id"),
                "label": record.get("subject"),
                "status": record.get("status"),
            })
    except Exception:  # noqa: BLE001
        logger.debug("Voice: task listing failed", exc_info=True)

    return {"ok": True, "agents": agents, "active_tasks": tasks}


def _approval_summary(request: Dict[str, Any]) -> str:
    """One short spoken-friendly line describing a permission request."""
    for key in ("title", "description", "display_name", "tool_name"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return "A worker agent needs permission to run a tool"


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
