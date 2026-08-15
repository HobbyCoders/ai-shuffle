"""Server-side sideband bridge for voice sessions (waves VW3–VW5, ruling VM8).

Transport shape (ruling VM2): the browser holds a WebRTC leg straight to
OpenAI — audio never transits this process — and the backend joins the SAME
realtime call over a *sideband* WebSocket
(``wss://api.openai.com/v1/realtime?call_id=...``). That leg is where the
backend owns instructions, VAD config and (from VW4) the tool declarations the
client can neither see nor tamper with, and where both sides' final transcripts
arrive for durable persistence.

Sideband handshake (corrected 2026-08-14 after a live 404): the URL is right and
unchanged, but a single attempt with the provider API key is NOT enough in
practice. ``_open_sideband`` now walks a credential ladder — the real API key
first (what the docs prescribe), then the ephemeral client secret the browser
used to create the call (what every reported fix in the wild uses) — and retries
the "call not found yet" class of handshake failure with a short backoff,
because the call takes a moment to become joinable after the SDP answer. Every
failed attempt is logged with the upstream HTTP status and body so a dead
handshake is diagnosable from the app log instead of a bare exception.

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

VW5 hardens the whole thing:

* **The call-length cap spans reconnects.** The registry stamps a per-session
  *call epoch* on the first attach; a replace-attach (the FE's 50-minute
  re-handshake) PRESERVES it and only an explicit hangup clears it, so the
  bridge deadline is ``max_call_minutes`` measured from the epoch — not from
  the attachment. When the cap fires the session is marked capped for a short
  grace window and the call/secret routes answer 409, so an auto-reconnect
  cannot silently restart the clock.
* **Deterministic default agent + a roster in the instructions**, so "just get
  an agent on it" always picks the same worker and the model knows the names
  it may pass.
* **Context retention** (``truncation.retention_ratio`` 0.8) sent as a SECOND,
  isolated ``session.update`` — a rejected shape can never cost us the
  instructions / VAD / tools of the first one.
* **Per-call usage + a cost estimate**, accumulated across replace-attaches on
  the epoch record and exposed through ``GET /voice/sessions/{id}/state``.

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
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# The sideband endpoint: same realtime call as the browser's WebRTC leg, joined
# by call_id. Verified current on 2026-08-14 against OpenAI's "Webhooks and
# server-side controls" guide and the officially-generated openai-python client
# (``client.realtime.connect(call_id=...)`` builds exactly this URL and sends
# only the Authorization header — no OpenAI-Beta, no subprotocol).
SIDEBAND_URL = "wss://api.openai.com/v1/realtime"

# How many times the whole credential ladder is walked before giving up, and how
# long to wait between walks. A freshly answered WebRTC call is not instantly
# joinable — the official SDK ships a reconnecting client and OpenAI's own codex
# retries its sideband join with backoff for the same reason.
SIDEBAND_CONNECT_ATTEMPTS = 3
SIDEBAND_RETRY_DELAYS: Tuple[float, ...] = (0.75, 1.5)

# Handshake statuses worth another attempt. 404 (``call_id_not_found``) leads the
# list deliberately: that is what a not-yet-registered call answers, and it is
# the exact failure this ladder exists to survive. Anything else (401/403 — wrong
# credential, 400 — wrong URL shape) is final for that credential and retrying it
# only delays the error the user needs to see.
SIDEBAND_RETRYABLE_STATUSES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})

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

# Default per-mode appendices for native-app sessions (the Android voice
# assistant). A native client reports its launch mode on call attach
# (``session_mode``); browser calls send none and get the base prompt only.
# All three prompts (the base VOICE_INSTRUCTIONS and these two) are
# runtime-overridable via Settings → Voice (voice_instructions*), stored in
# system_settings; these constants are the fallbacks and the UI's
# reset-to-default text.
DEFAULT_COMMAND_INSTRUCTIONS = (
    "You are also the user's hands-free phone assistant, speaking through "
    "their Android phone, and this session was started by wake word — the "
    "user may be driving and cannot touch the phone. You can act on the "
    "phone with the device tools the session declares (calls, texts, "
    "notifications, apps, navigation, alarms, timers, calendar, "
    "end_session). Handle the request quickly, by voice alone, then say a "
    "very short confirmation and call end_session — do not ask 'anything "
    "else?' and do not keep the session open. If the user stays silent, "
    "call end_session. Execute actions immediately without asking for "
    "confirmation — EXCEPT send_sms: its body is transcribed speech, so "
    "first call it without confirmed=true, read the message back in one "
    "short sentence, and send with confirmed=true only after a spoken yes. "
    "If a contact lookup is ambiguous the tool will say so — ask which one, "
    "briefly. If a tool reports a missing permission, say so in a few words "
    "and move on. NEVER speak first: stay completely silent until the user "
    "says something. Keep every reply to ONE short sentence — no greetings, "
    "no explanations of what you are doing."
)

DEFAULT_CHAT_INSTRUCTIONS = (
    "You are also the user's phone assistant, speaking through their "
    "Android phone. You can act on the phone with the device tools the "
    "session declares (calls, texts, notifications, apps, navigation, "
    "alarms, timers, calendar, end_session). Execute actions immediately "
    "without asking for confirmation — EXCEPT send_sms: its body is "
    "transcribed speech, so first call it without confirmed=true, read the "
    "message back in one short sentence, and send with confirmed=true only "
    "after a spoken yes. If a contact lookup is ambiguous the tool will say "
    "so — ask which one, briefly. If a tool reports a missing permission, "
    "say so in a few words and move on. NEVER speak first: stay silent "
    "until the user says something. Keep replies short and spoken-style. "
    "When the user says goodbye, \"that's all\", or asks you to hang up, "
    "say a short goodbye and call end_session."
)

#: session_mode → default appendix (the route composes stored overrides).
DEFAULT_MODE_INSTRUCTIONS: Dict[str, str] = {
    "command": DEFAULT_COMMAND_INSTRUCTIONS,
    "chat": DEFAULT_CHAT_INSTRUCTIONS,
}

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
                        "Optional. The name of one of the agents listed in your "
                        "instructions under 'Agents you can dispatch'. Call "
                        "list_agents if you are unsure which names are valid. "
                        "Omit it to use the default worker agent."
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

# Context retention (ruling VM8), sent as its OWN ``session.update`` — see
# ``_send_truncation_update``. Shape verified against the OpenAI realtime docs
# (Aug 2026): drop the oldest items only once the input-token limit is hit, and
# then down to 80% of it, which amortises truncations across turns and keeps the
# prompt cache warm.
TRUNCATION_CONFIG: Dict[str, Any] = {
    "type": "retention_ratio",
    "retention_ratio": 0.8,
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

# The roster appended to the instructions at attach time (VW5). Named agents in
# the prompt are what make ``dispatch_agent(agent=...)`` land on a real target
# instead of a hallucinated one; the cap keeps a big profile list from crowding
# out the contract itself.
ROSTER_HEADER = "Agents you can dispatch:"
ROSTER_MAX_CHARS = 1200

# Agent id/name that wins the default-target pick outright (case-insensitive).
DEFAULT_AGENT_NAME = "default"

# Per-1M-token list prices for the three allowlisted realtime models, used for
# the ESTIMATE surfaced in /state. Cached-input discounts are deliberately
# ignored (the estimate reads high rather than low), and an unknown model simply
# yields no estimate rather than a wrong one.
MODEL_PRICES_PER_MTOK: Dict[str, Dict[str, float]] = {
    "gpt-realtime-2.1": {
        "text_in": 4.0, "text_out": 24.0, "audio_in": 32.0, "audio_out": 64.0,
    },
    "gpt-realtime-2.1-mini": {
        "text_in": 0.60, "text_out": 2.40, "audio_in": 10.0, "audio_out": 20.0,
    },
    "gpt-realtime": {
        "text_in": 4.0, "text_out": 16.0, "audio_in": 32.0, "audio_out": 64.0,
    },
}

# How long after the duration cap fires the session stays "capped": inside this
# window /call and /secret answer 409 so the FE's automatic reconnect cannot
# start a fresh clock without the user hanging up first.
CAP_GRACE_SECONDS = 300.0


# ============================================================================
# Sideband transport
# ============================================================================

def _handshake_failure(exc: BaseException) -> Tuple[Optional[int], str]:
    """``(HTTP status, detail)`` for a failed WebSocket handshake.

    ``websockets`` raises ``InvalidStatus`` carrying the whole rejected HTTP
    response; older revisions raised ``InvalidStatusCode`` with a bare
    ``status_code``. Both are read defensively — the body is where OpenAI puts
    the machine-readable reason (``call_id_not_found`` vs ``action_not_found``
    vs an auth error), and losing it is what made this bug take a live probe to
    diagnose in the first place.
    """
    status: Optional[int] = None
    detail = ""

    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            status = code
        body = getattr(response, "body", None)
        if isinstance(body, (bytes, bytearray)):
            detail = bytes(body).decode("utf-8", "replace").strip()[:500]
        elif isinstance(body, str):
            detail = body.strip()[:500]
    if status is None:
        code = getattr(exc, "status_code", None)
        if isinstance(code, int):
            status = code
    if not detail:
        detail = str(exc)[:500]
    return status, detail


def _sideband_credentials(
    api_key: str, client_secret: Optional[str]
) -> List[Tuple[str, str]]:
    """The ``(label, bearer)`` ladder the sideband tries, in order.

    The ephemeral client secret leads: live validation (2026-08-14, real
    browser-created WebRTC call, gpt-realtime-2.1) showed the API key answers
    ``call_id_not_found`` on EVERY attempt while the secret that created the
    call connects on its FIRST — the docs' server-controls guide describes the
    SIP path, not browser-created calls. The API key stays as the fallback for
    exactly that SIP/other-call-shape case, and as the last resort should the
    secret have expired mid-call (the mint TTL is sized to outlive the call cap
    precisely so that stays theoretical).
    """
    ladder: List[Tuple[str, str]] = []
    seen: set = set()
    for label, value in (("client_secret", client_secret), ("api_key", api_key)):
        value = (value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ladder.append((label, value))
    if not ladder:
        raise RuntimeError("no credential is available for the voice sideband")
    return ladder


async def _open_sideband(
    call_id: str, api_key: str, client_secret: Optional[str] = None
):
    """Open the authenticated sideband WebSocket for ``call_id``.

    Split out as a module-level function so tests can substitute an in-process
    fake — nothing else in this module knows about the ``websockets`` package.

    Walks :func:`_sideband_credentials` on each attempt and retries the whole
    ladder while the failures stay in :data:`SIDEBAND_RETRYABLE_STATUSES`. The
    exception that finally propagates is the last real one, so the ``/call``
    route still turns a dead handshake into its 502.
    """
    # Imported lazily: the package ships with uvicorn[standard], but a missing
    # optional extra must fail the call, not the whole app import.
    from websockets.asyncio.client import connect

    url = f"{SIDEBAND_URL}?call_id={quote(call_id, safe='')}"
    ladder = _sideband_credentials(api_key, client_secret)
    last_exc: Optional[BaseException] = None

    for attempt in range(SIDEBAND_CONNECT_ATTEMPTS):
        retryable = False
        for label, bearer in ladder:
            try:
                socket = await connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {bearer}"},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — classified just below
                last_exc = exc
                status, detail = _handshake_failure(exc)
                retryable = retryable or status is None or (
                    status in SIDEBAND_RETRYABLE_STATUSES
                )
                logger.warning(
                    "Voice: sideband handshake for call %s rejected with %s using "
                    "the %s (attempt %s/%s): %s",
                    call_id, status if status is not None else "no HTTP status",
                    label, attempt + 1, SIDEBAND_CONNECT_ATTEMPTS, detail,
                )
                continue
            if label != "api_key":
                logger.info(
                    "Voice: sideband for call %s attached using the %s "
                    "(the provider API key was rejected)", call_id, label,
                )
            return socket

        if not retryable or attempt >= SIDEBAND_CONNECT_ATTEMPTS - 1:
            break
        delay = SIDEBAND_RETRY_DELAYS[min(attempt, len(SIDEBAND_RETRY_DELAYS) - 1)]
        await asyncio.sleep(delay)

    if last_exc is None:  # pragma: no cover — the ladder is never empty
        raise RuntimeError("the voice sideband made no connection attempt")
    raise last_exc


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
        client_secret: Optional[str] = None,
        call_started_at: Optional[float] = None,
        usage_totals: Optional[Dict[str, int]] = None,
        on_duration_cap: Optional[Callable[[], None]] = None,
        client_tools: Optional[List[Dict[str, Any]]] = None,
        client_instructions: Optional[str] = None,
        instructions_override: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.call_id = call_id
        self.model = model
        self.max_call_minutes = max_call_minutes
        self._api_key = api_key
        # The ephemeral credential the browser created THIS call with, when the
        # route still has it. Only ever used as the sideband's fallback bearer;
        # it is never logged and never leaves the process.
        self._client_secret = client_secret

        # When the CALL (not this attachment) began. The registry passes the
        # session's epoch so a replace-attach keeps counting down the same cap;
        # a directly-constructed bridge just starts its own clock.
        self.call_started_at: float = (
            call_started_at if call_started_at is not None else time.time()
        )
        # Cumulative realtime token usage for the whole call. The registry hands
        # in the epoch's dict so totals survive a replace-attach.
        self.usage: Dict[str, int] = usage_totals if usage_totals is not None else {}
        self._on_duration_cap = on_duration_cap

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

        # -- client-serviced tools (native app integration) ----------------
        # Tools a native client (e.g. the Android app) declared on attach and
        # services ITSELF over its own data channel: we declare them in our
        # session.update so the model can call them, and we must NOT answer
        # them — a second function_call_output for the same call would race
        # the client's real result with our "Unknown tool" error.
        self._client_tools: List[Dict[str, Any]] = list(client_tools or [])
        self._client_tool_names: set = {
            t.get("name") for t in self._client_tools if t.get("name")
        }
        self._client_instructions = (client_instructions or "").strip()
        # The route-composed prompt (settings overrides + mode appendix).
        # None → the built-in VOICE_INSTRUCTIONS.
        self._instructions_override = (instructions_override or "").strip() or None

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
            "usage": dict(self.usage),
            "estimated_cost_usd": estimate_cost_usd(self.model, self.usage),
        }

    async def start(self) -> None:
        """Connect, configure the realtime session, and start reading events.

        Raises whatever the transport raises — the caller turns a failed connect
        into a 502 rather than registering a dead bridge.
        """
        self._ws = await _open_sideband(
            self.call_id, self._api_key, self._client_secret,
        )
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

        # Deliberately a SECOND update: the retention knob is the one field whose
        # shape we cannot re-verify from inside a call, and bundling it would let
        # a schema rejection take the instructions, tools and VAD config with it.
        try:
            await self._send_truncation_update()
        except Exception:
            logger.warning(
                "Voice: truncation session.update failed for %s (default "
                "truncation applies)", self.session_id, exc_info=True,
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

    def remaining_seconds(self) -> float:
        """Seconds left on the call cap, measured from the CALL's epoch (VW5).

        Reconnecting mid-call re-attaches a new bridge to the same epoch, so the
        deadline keeps counting down instead of starting over — the bug this
        wave exists to fix.
        """
        window = max(int(self.max_call_minutes or 0), 0) * SECONDS_PER_MINUTE
        return window - (time.time() - self.call_started_at)

    async def _duration_cap(self) -> None:
        """Close the call once it exceeds ``voice_max_call_minutes`` (VM13)."""
        try:
            await asyncio.sleep(max(self.remaining_seconds(), 0.0))
        except asyncio.CancelledError:
            raise
        logger.warning(
            "Voice: session %s hit the %s-minute call cap — closing the sideband",
            self.session_id, self.max_call_minutes,
        )
        # Mark the session capped BEFORE stopping: the FE reconnects on drop, and
        # the grace window is what turns that reconnect into a 409 instead of a
        # brand-new clock.
        if self._on_duration_cap is not None:
            try:
                self._on_duration_cap()
            except Exception:  # noqa: BLE001 — never let bookkeeping kill teardown
                logger.debug(
                    "Voice: cap bookkeeping failed for %s", self.session_id,
                    exc_info=True,
                )
        await self.stop(reason="duration_cap")

    # -- outbound ----------------------------------------------------------

    async def _send_session_update(self) -> None:
        """Send the initial ``session.update`` (instructions, tools, turn detection).

        The instruction payload is the VM16 contract text VERBATIM, then the
        dispatchable-agent roster (VW5), then the history seed — in that order,
        so the contract always leads and the two appendices can be trimmed
        independently.

        Retention/truncation is NOT here on purpose: see
        :meth:`_send_truncation_update`.
        """
        instructions = self._instructions_override or VOICE_INSTRUCTIONS
        if self._client_instructions:
            # The native client's appendix (device tools like calls/SMS) rides
            # after the contract so the contract always leads.
            instructions = f"{instructions}\n\n{self._client_instructions}"
        roster = await self._agent_roster()
        if roster:
            instructions = f"{instructions}\n\n{roster}"
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
                # Client-declared device tools are appended verbatim (already
                # sanitized by the route) and serviced by the client itself.
                "tools": [dict(tool) for tool in VOICE_TOOLS]
                + [dict(tool) for tool in self._client_tools],
                "audio": {"input": {"turn_detection": dict(TURN_DETECTION_CONFIG)}},
            },
        })

    async def _send_truncation_update(self) -> None:
        """Send the retention config as its own, isolated ``session.update``.

        Ruling VM8 wants ``retention_ratio`` 0.8; the documented shape is
        ``session.truncation = {"type": "retention_ratio", "retention_ratio": ...}``
        (OpenAI realtime docs, Aug 2026). It travels alone so that a schema
        rejection costs only the retention knob — never the instructions, tools
        or VAD config of the first update. A rejection arrives asynchronously as
        an ``error`` event and is logged by :meth:`_handle_event`.
        """
        await self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "truncation": dict(TRUNCATION_CONFIG),
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
            usage = usage if isinstance(usage, dict) else {}
            self._accumulate_usage(usage)
            await self._flush_exchange(usage)
            return

        if etype == "error":
            detail = str(event.get("error"))[:500]
            if "truncation" in detail.lower() or "retention" in detail.lower():
                # The isolated second update was rejected: the call runs on the
                # API's default truncation and everything else is unaffected.
                logger.warning(
                    "Voice: the retention/truncation config was rejected for %s "
                    "(default truncation applies): %s", self.session_id, detail,
                )
                return
            logger.warning(
                "Voice: sideband reported an error for %s: %s",
                self.session_id, detail,
            )
            return

        logger.debug(
            "Voice: ignoring sideband event type=%r for %s", etype, self.session_id,
        )

    # -- usage accounting (VW5) --------------------------------------------

    def _accumulate_usage(self, usage: Dict[str, Any]) -> None:
        """Fold one ``response.done`` usage block into the call's totals.

        Additive into the dict the registry owns, so a replace-attach keeps
        counting the same call rather than starting from zero.
        """
        for key, value in _usage_deltas(usage).items():
            self.usage[key] = self.usage.get(key, 0) + value

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
        if name in self._client_tool_names:
            # A client-serviced (device) tool: the native app answers it over
            # its own data channel. Answering here too would race the app's
            # real result with an "Unknown tool" error for the same call_id.
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
        """Agent to use when the model omits ``agent`` — DETERMINISTIC (VW5).

        Two rules, in order: an agent whose id or name is literally "default"
        (case-insensitive), else the alphabetically first eligible agent by
        name. "Eligible" excludes the voice session's own profile, because
        dispatching the caller's own agent is refused as a cycle by
        ``task_dispatch``.

        Stability is the point: "just get an agent on it" must land on the same
        worker every time, not on whatever the profile table happened to return
        first. Returns None when there is nothing to pick, and the dispatch then
        fails with task_dispatch's own "needs a target" error.
        """
        caller = await self._caller_profile_id()
        rows = await run_sync(_eligible_agent_rows, caller)
        return _pick_default_agent(rows)

    async def _agent_roster(self) -> str:
        """The "Agents you can dispatch:" block appended to the instructions.

        Built from the SAME resolver the default pick and dispatch use, so a
        name the model reads back is always a name dispatch accepts.
        """
        caller = await self._caller_profile_id()
        rows = await run_sync(_eligible_agent_rows, caller)
        return _agent_roster_text(rows)

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

        Already-answered tolerance (VM23): the GUI modal is suppressed during a
        live call, but any OTHER surface (another device, the call ending and
        the modal returning) can still answer first. When the pending row is
        gone, look the request up among the ANSWERED rows and report that
        outcome as a success — the model should tell the user it is already
        handled and move on, not apologise about a missing request id.
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

        if isinstance(result, dict) and result.get("error"):
            answered = await run_sync(
                _answered_approval_decision, self.session_id, request_id,
            )
            if answered:
                return {
                    "ok": True,
                    "request_id": request_id,
                    "decision": answered,
                    "note": (
                        f"This request was already answered ({answered}) by the "
                        "user in the app — nothing more to do; the worker is "
                        "proceeding accordingly."
                    ),
                }
            return {
                "ok": False,
                "request_id": request_id,
                "error": (
                    f"No pending approval with id {request_id} — it may have "
                    "expired or been withdrawn."
                ),
            }

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

        This is the spoken leg of ruling VM11 — and during a live call it is
        the ONLY leg: the FE suppresses the GUI permission modal while the call
        surface is up (VM23), so the same request is never presented on two
        surfaces at once. Nothing here answers a prompt: only
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


def _eligible_agent_rows(caller_profile_id: Optional[str]) -> List[Dict[str, Any]]:
    """Agent definitions this voice session may dispatch to.

    The caller's own profile is excluded — ``task_dispatch`` refuses it as a
    cycle, so offering it would only produce a failed dispatch the model has to
    explain out loud.
    """
    try:
        rows = database.get_all_profiles() or []
    except Exception:  # noqa: BLE001
        logger.debug("Voice: agent listing failed", exc_info=True)
        return []
    return [
        row for row in rows
        if row.get("id") and row.get("id") != caller_profile_id
    ]


def _agent_sort_key(row: Dict[str, Any]) -> Tuple[str, str]:
    """Stable alphabetical order by display name, id as the tiebreak."""
    name = (row.get("name") or row.get("id") or "").strip().lower()
    return (name, str(row.get("id") or ""))


def _pick_default_agent(rows: List[Dict[str, Any]]) -> Optional[str]:
    """Rule 1: an agent literally called "default". Rule 2: first by name."""
    for row in rows:
        for key in ("id", "name"):
            value = row.get(key)
            if isinstance(value, str) and value.strip().lower() == DEFAULT_AGENT_NAME:
                return row["id"]
    ordered = sorted(rows, key=_agent_sort_key)
    return ordered[0]["id"] if ordered else None


def _first_sentence(value: Any) -> str:
    """First sentence of a description, whitespace-collapsed and bounded."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0][:AGENT_DESCRIPTION_MAX_CHARS]


def _agent_roster_text(rows: List[Dict[str, Any]]) -> str:
    """One line per dispatchable agent, hard-capped at ``ROSTER_MAX_CHARS``."""
    lines: List[str] = []
    used = len(ROSTER_HEADER)
    for row in sorted(rows, key=_agent_sort_key)[:MAX_LISTED_AGENTS]:
        name = (row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        summary = _first_sentence(row.get("description"))
        line = f"- {name} — {summary}" if summary else f"- {name}"
        if used + len(line) + 1 > ROSTER_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return ROSTER_HEADER + "\n" + "\n".join(lines)


def _usage_deltas(usage: Dict[str, Any]) -> Dict[str, int]:
    """Flatten one realtime usage block into the counters we accumulate.

    Only fields actually present are returned — the realtime schema has grown
    subfields over revisions and an absent one must not read as a zero.
    """
    out: Dict[str, int] = {}

    def _put(key: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if value:
            out[key] = int(value)

    if not isinstance(usage, dict):
        return out
    _put("total_tokens", usage.get("total_tokens"))
    _put("input_tokens", usage.get("input_tokens"))
    _put("output_tokens", usage.get("output_tokens"))

    details_in = usage.get("input_token_details")
    if isinstance(details_in, dict):
        _put("input_text_tokens", details_in.get("text_tokens"))
        _put("input_audio_tokens", details_in.get("audio_tokens"))
        _put("input_cached_tokens", details_in.get("cached_tokens"))
    details_out = usage.get("output_token_details")
    if isinstance(details_out, dict):
        _put("output_text_tokens", details_out.get("text_tokens"))
        _put("output_audio_tokens", details_out.get("audio_tokens"))
    return out


def estimate_cost_usd(model: Optional[str], usage: Dict[str, int]) -> Optional[float]:
    """Rough per-call spend for ``usage``, or ``None`` for an unpriced model.

    An ESTIMATE, and labelled as one everywhere it surfaces: cached-input
    discounts are ignored (so it reads high, never low) and text/audio are split
    from the token details when present, falling back to "whatever isn't audio
    is text".
    """
    prices = MODEL_PRICES_PER_MTOK.get((model or "").strip())
    if not prices or not usage:
        return None

    audio_in = int(usage.get("input_audio_tokens") or 0)
    audio_out = int(usage.get("output_audio_tokens") or 0)
    text_in = usage.get("input_text_tokens")
    if text_in is None:
        text_in = max(int(usage.get("input_tokens") or 0) - audio_in, 0)
    text_out = usage.get("output_text_tokens")
    if text_out is None:
        text_out = max(int(usage.get("output_tokens") or 0) - audio_out, 0)

    total = (
        int(text_in) * prices["text_in"]
        + int(text_out) * prices["text_out"]
        + audio_in * prices["audio_in"]
        + audio_out * prices["audio_out"]
    ) / 1_000_000
    return round(total, 6)


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


def _answered_approval_decision(session_id: str, request_id: str) -> Optional[str]:
    """The decision an ALREADY-answered permission request settled on, if any.

    Synchronous (call via ``run_sync``). Backs the VM23 already-answered
    tolerance in ``respond_approval``: a request the user resolved from another
    surface should read back as its outcome, not as "not found".
    """
    try:
        from app.core.permission_handler import PERMISSION_KIND
        from app.db import interactions

        for row in interactions.get_answered_interactions(session_id, PERMISSION_KIND):
            payload = row.get("payload") or {}
            if payload.get("request_id") != request_id:
                continue
            decision = (row.get("answer") or {}).get("decision")
            if isinstance(decision, str) and decision:
                return decision
    except Exception:  # noqa: BLE001 — a failed lookup just keeps the error path
        logger.debug(
            "Voice: answered-approval lookup failed for %s", session_id,
            exc_info=True,
        )
    return None


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

class _CallEpoch:
    """What survives a re-attachment: when the CALL started, and its usage.

    One per session, stamped on the first attach, carried through every
    replace-attach, dropped on hangup (or when the duration cap fires). It is
    what makes "50 minutes in, re-handshake" continue the same call instead of
    starting a new one.
    """

    __slots__ = ("started_at", "usage")

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.usage: Dict[str, int] = {}


class VoiceCallRegistry:
    """In-process map of ``session_id -> VoiceBridge`` (ruling VM8)."""

    def __init__(self) -> None:
        self._bridges: Dict[str, VoiceBridge] = {}
        self._epochs: Dict[str, _CallEpoch] = {}
        # session_id -> (when the cap fired, the cap in minutes)
        self._capped: Dict[str, Tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    def get(self, session_id: str) -> Optional[VoiceBridge]:
        return self._bridges.get(session_id)

    def call_started_at(self, session_id: str) -> Optional[float]:
        """When the session's current call began, across re-attachments."""
        epoch = self._epochs.get(session_id)
        return epoch.started_at if epoch else None

    # -- duration-cap grace window -----------------------------------------

    def mark_capped(self, session_id: str, max_call_minutes: int) -> None:
        """Record that ``session_id`` just hit its call-length cap.

        Also drops the epoch: the only way forward is an explicit hangup and a
        NEW call, so the next attach (once the grace window has expired) must
        start a fresh clock rather than inherit an already-blown one.
        """
        self._capped[session_id] = (time.time(), int(max_call_minutes or 0))
        self._epochs.pop(session_id, None)

    def capped_minutes(self, session_id: str) -> Optional[int]:
        """The cap in minutes while the grace window holds, else ``None``."""
        entry = self._capped.get(session_id)
        if entry is None:
            return None
        capped_at, minutes = entry
        if time.time() - capped_at > CAP_GRACE_SECONDS:
            self._capped.pop(session_id, None)
            return None
        return minutes

    def clear_capped(self, session_id: str) -> None:
        """Forget the cap — the user hung up, so a new call is allowed."""
        self._capped.pop(session_id, None)

    def state(self, session_id: str) -> Dict[str, Any]:
        """State payload for a session, live bridge or not."""
        bridge = self._bridges.get(session_id)
        if bridge is None:
            return {
                "active": False, "connected": False,
                "started_at": None, "model": None,
                "usage": {}, "estimated_cost_usd": None,
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
        client_secret: Optional[str] = None,
        client_tools: Optional[List[Dict[str, Any]]] = None,
        client_instructions: Optional[str] = None,
        instructions_override: Optional[str] = None,
    ) -> VoiceBridge:
        """Attach the sideband, REPLACING any existing attachment.

        Replacement (rather than rejection) is what makes the FE's reconnect
        near OpenAI's session wall work: the FE re-handshakes with a fresh
        ``call_id`` and posts it here, the old attachment is closed, and the new
        one reseeds context from the persisted transcript.

        The session's call EPOCH (start time + usage totals) is preserved across
        that replacement, so the duration cap and the cost estimate span the
        whole call rather than restarting with each attachment (VW5). Only a
        hangup — or the cap itself — clears it.

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

            epoch = self._epochs.get(session_id) or _CallEpoch(time.time())
            bridge = VoiceBridge(
                session_id, call_id,
                api_key=api_key, model=model, max_call_minutes=max_call_minutes,
                client_secret=client_secret,
                call_started_at=epoch.started_at,
                usage_totals=epoch.usage,
                on_duration_cap=lambda: self.mark_capped(
                    session_id, max_call_minutes,
                ),
                client_tools=client_tools,
                client_instructions=client_instructions,
                instructions_override=instructions_override,
            )
            # Only record the epoch once the connect actually succeeded — a
            # failed attach must not leave a clock running for a call that never
            # started.
            await bridge.start()
            self._epochs[session_id] = epoch
            self._bridges[session_id] = bridge
            return bridge

    async def detach(self, session_id: str) -> bool:
        """Stop and drop the session's bridge. ``False`` when there was none.

        This is the explicit end of a call: the epoch and any cap marker go with
        it, so the next call starts a fresh clock.
        """
        async with self._lock:
            bridge = self._bridges.pop(session_id, None)
            self._epochs.pop(session_id, None)
            self._capped.pop(session_id, None)
        if bridge is None:
            return False
        await bridge.stop(reason="hangup")
        return True

    async def shutdown_all(self) -> None:
        """Close every live bridge (app shutdown). Never raises."""
        async with self._lock:
            bridges = list(self._bridges.values())
            self._bridges.clear()
            self._epochs.clear()
            self._capped.clear()
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
