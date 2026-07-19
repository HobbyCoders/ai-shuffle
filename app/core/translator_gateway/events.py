"""
Provider-agnostic normalized stream model for the translator gateway.

This is the single internal event/model layer that sits between *any* upstream
provider's streaming protocol and the Anthropic SSE we hand back to Claude Code
(plan §A.3 "Normalize into one internal event model at the boundary" / F5 / T3).

    Upstream SSE  ──parse──▶  [these normalized events]  ──serialize──▶  Anthropic SSE

Today only the OpenAI-Responses parser (``responses_to_anthropic.py``) feeds this
model, but the contract is deliberately provider-agnostic: a future provider adds
its own ``<x>_to_normalized`` parser and reuses the normalized→Anthropic
serializer unchanged (additive, no rewrite).

Style note: these are plain ``dataclass`` types to match the prevailing app/core
convention (see ``app/core/provider/types.py``); the wider codebase uses pydantic
for request/response *schemas*, but the internal event bus here is dataclasses.

No network, no I/O, no credentials — pure data + a tiny base64/JSON codec for the
reasoning round-trip signature envelope (§D4).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─── Reasoning round-trip signature envelope (§D4) ───────────────────────────
#
# OpenAI Responses reasoning items carry an opaque ``encrypted_content`` blob
# that the Codex backend accepts back verbatim across stateless full-history
# resends (spike E4 — accepted HTTP 200 on ≥2 resends). Claude Code, however,
# only round-trips *Anthropic* ``thinking`` blocks, and it resends a thinking
# block's ``signature`` field verbatim. So we smuggle everything needed to
# rebuild the reasoning item through that signature: base64 of a small JSON
# envelope. anthropic_to_responses reverses it.
#
# Reference contrast (raine/claude-code-proxy): raine does NOT round-trip Codex
# reasoning at all — it DROPS upstream reasoning blocks and instead relies on
# stateful, in-memory session-keyed continuation (maps a session id → upstream
# conversation id). That is a fundamentally different, *stateful* design. Our
# gateway is stateless per turn (Claude Code resends full history every turn),
# so a stateful side-table would desync; the signature envelope keeps the
# reasoning item's latent state riding along with the history CC already resends.
# We keep our envelope regardless of raine's choice.

ENVELOPE_VERSION = 1


def encode_reasoning_signature(
    encrypted_content: Optional[str],
    summary: Optional[List[Any]] = None,
    content: Optional[List[Any]] = None,
) -> str:
    """Pack a Responses reasoning item into an Anthropic ``signature`` string.

    Everything needed to reconstruct the reasoning input item verbatim *minus*
    ``id``/``status`` (both stripped on resend, §surprise #6) is stored: the
    ``encrypted_content`` blob plus the ``summary`` and ``content`` arrays.

    Deterministic: same inputs → same signature (compact JSON, stable key order).
    """
    envelope = {
        "v": ENVELOPE_VERSION,
        "encrypted_content": encrypted_content,
        "summary": summary if summary is not None else [],
        "content": content if content is not None else [],
    }
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_reasoning_signature(signature: Optional[str]) -> Optional[Dict[str, Any]]:
    """Reverse :func:`encode_reasoning_signature`.

    Returns the envelope dict for a signature *we* produced, or ``None`` for any
    signature we did not (native-Claude thinking blocks carry a real Anthropic
    cryptographic signature — a foreign, opaque base64 string that will not
    decode to our JSON envelope). A ``None`` result is the caller's signal to
    DROP the thinking block rather than fabricate a reasoning item (§D4).
    """
    if not signature or not isinstance(signature, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(signature.encode("ascii"))
        envelope = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("v") != ENVELOPE_VERSION:
        return None
    if "encrypted_content" not in envelope:
        return None
    return envelope


# ─── Normalized streaming events ─────────────────────────────────────────────
#
# The provider-agnostic stream. Text and thinking block boundaries are implicit
# (the serializer opens a block on the first delta for a new item and closes it
# on transition / end); tool calls carry explicit Start/ArgsDelta/End because a
# tool block's byte-exact argument accumulation is the critical fidelity surface
# (§D3a, LiteLLM #25321 is the failure to avoid).

@dataclass
class NormalizedEvent:
    """Base class for normalized stream events. Not instantiated directly."""
    pass


@dataclass
class MessageStart(NormalizedEvent):
    """The assistant turn is beginning. Carries the routing/echo metadata the
    Anthropic ``message_start`` frame needs."""
    message_id: str
    model: Optional[str] = None
    role: str = "assistant"


@dataclass
class TextDelta(NormalizedEvent):
    """A fragment of assistant output text, keyed by upstream item id so the
    serializer can group deltas into one Anthropic text content block."""
    item_id: str
    text: str


@dataclass
class ToolCallStart(NormalizedEvent):
    """A function/tool call block is opening.

    ``call_id`` is the join key preserved across the whole round-trip (it becomes
    the Anthropic ``tool_use.id`` and, on resend, the ``function_call_output``
    join key, §surprise #6)."""
    item_id: str
    call_id: str
    name: str


@dataclass
class ToolCallArgsDelta(NormalizedEvent):
    """One raw fragment of the tool-call arguments JSON string.

    Fragments MUST be re-emitted byte-for-byte (they concatenate to the exact
    arguments string — spike E2/E3 verified byte-exact reassembly). Any
    ``obfuscation`` padding on the upstream event has already been dropped."""
    item_id: str
    delta: str


@dataclass
class ToolCallEnd(NormalizedEvent):
    """A tool call block is closing. ``arguments`` is the authoritative full
    arguments string (from ``function_call_arguments.done`` /
    ``output_item.done``)."""
    item_id: str
    arguments: str


@dataclass
class ThinkingDelta(NormalizedEvent):
    """A fragment of reasoning *summary* text (the raw chain stays opaque inside
    ``encrypted_content``). Rendered as an Anthropic ``thinking_delta``."""
    item_id: str
    text: str


@dataclass
class ThinkingEnd(NormalizedEvent):
    """A thinking/reasoning block is closing. ``signature`` carries the reasoning
    round-trip envelope (§D4) and is emitted as an Anthropic ``signature_delta``
    before the block stop."""
    item_id: str
    signature: str


@dataclass
class UsageUpdate(NormalizedEvent):
    """Token accounting, normalized to Anthropic-shaped fields.

    ``reasoning_tokens`` is a SUBSET of ``output_tokens`` (§D3c) — carried for
    internal accounting only, never added into the Anthropic output count and
    never surfaced as an Anthropic usage field."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass
class MessageEnd(NormalizedEvent):
    """The assistant turn is complete. ``stop_reason`` is already mapped to the
    Anthropic vocabulary (``end_turn`` / ``tool_use`` / ``max_tokens``)."""
    stop_reason: str = "end_turn"


# ─── Final-item structures (authoritative, from output_item.done) ────────────
#
# Accumulated from ``response.output_item.done`` events, NEVER from
# ``response.completed.output`` (always empty — §surprise #2). These back the
# buffered / non-streaming assembly path (stage 2 wires it) and let callers
# reconstruct the full turn without replaying the SSE.

@dataclass
class NormalizedUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass
class FinalTextItem:
    """An authoritative assistant text block."""
    text: str


@dataclass
class FinalToolCallItem:
    """An authoritative function/tool call."""
    call_id: str
    name: str
    arguments: str  # verbatim JSON string


@dataclass
class FinalReasoningItem:
    """An authoritative reasoning item plus the derived signature envelope."""
    encrypted_content: Optional[str]
    summary: List[Any] = field(default_factory=list)
    content: List[Any] = field(default_factory=list)
    signature: str = ""


@dataclass
class NormalizedResponse:
    """The whole assembled turn: ordered final items + usage + stop reason."""
    items: List[Any] = field(default_factory=list)
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    stop_reason: str = "end_turn"
    model: Optional[str] = None
    message_id: Optional[str] = None
