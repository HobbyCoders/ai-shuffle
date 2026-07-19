"""
Anthropic Messages request  →  OpenAI Responses request translation (§A.2 / §D2).

Pure, synchronous, no I/O. Given an Anthropic ``/v1/messages`` request body this
produces the JSON body for ``POST .../responses`` per the live-validated spike
template (docs/CODEX_SPIKE_FINDINGS.md "Working request template").

Binding contract points from the spike honored here:
  * ``store:false``, ``stream:true`` always (non-stream is 400 — surprise #3).
  * ``include:["reasoning.encrypted_content"]`` always (so reasoning items come
    back with the blob we round-trip — §D4 / E4).
  * On resend, every reconstructed input item has ``id`` AND ``status`` stripped
    (surprise #6) — we simply never emit them.
  * ``max_output_tokens`` is OMITTED for the Codex upstream (surprise #7); it is
    only included when ``upstream_mode="openai"`` (api.openai.com), where the
    caller (stage 2) may want it.
  * Thinking blocks reverse the §D4 signature envelope back into reasoning input
    items; native-Claude thinking (no valid envelope) is DROPPED, never faked.

Reference: raine/claude-code-proxy translates request-direction reasoning *effort*
but drops reasoning items on the way back and keeps continuation state server-side
in memory; we round-trip the item statelessly via the signature envelope instead
(see events.py header).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.translator_gateway.events import decode_reasoning_signature


# ─── Reasoning effort mapping (§D2) ──────────────────────────────────────────
#
# Anthropic extended thinking is a *token budget*; OpenAI Responses reasoning is
# a coarse *effort* enum. Thresholds (documented, deliberately conservative):
#
#   budget_tokens < 4096          → "low"
#   4096 <= budget_tokens < 16384 → "medium"
#   budget_tokens >= 16384        → "high"
#
# 4096 is Anthropic's minimum enabled thinking budget; 16384 is a common
# "deep think" tier. Precision is not important (the backend normalizes reasoning
# config server-side anyway — surprise #5); monotonicity is.
_EFFORT_LOW_CEIL = 4096
_EFFORT_MEDIUM_CEIL = 16384


def _effort_from_budget(budget_tokens: Optional[int]) -> str:
    if budget_tokens is None:
        return "medium"
    if budget_tokens < _EFFORT_LOW_CEIL:
        return "low"
    if budget_tokens < _EFFORT_MEDIUM_CEIL:
        return "medium"
    return "high"


def _normalize_effort(effort: Any) -> str:
    """Normalize the CLI's ``output_config.effort`` to a Responses effort enum.

    Adaptive thinking (CLI 2.1.214) carries the requested effort in a top-level
    ``output_config: {"effort": ...}`` instead of a token budget. Valid Responses
    values pass through; "xhigh"/"max" clamp to "high"; anything unknown → "medium".
    """
    if effort in ("minimal", "low", "medium", "high"):
        return effort
    if effort in ("xhigh", "max"):
        return "high"
    return "medium"


# ─── system → instructions ───────────────────────────────────────────────────

def _system_to_instructions(system: Any) -> Optional[str]:
    """Anthropic ``system`` may be a string or a list of text blocks."""
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: List[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(system)


def _stringify_tool_result_content(content: Any) -> str:
    """A Responses ``function_call_output.output`` must be a plain string.

    Anthropic ``tool_result.content`` may be a string or a list of blocks; text
    blocks are concatenated, anything non-text is JSON-encoded so no signal is
    silently lost."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(json.dumps(block, separators=(",", ":"), ensure_ascii=False))
        return "".join(parts)
    return json.dumps(content, separators=(",", ":"), ensure_ascii=False)


def _normalize_message_content(content: Any) -> List[Dict[str, Any]]:
    """Anthropic message ``content`` may be a bare string; normalize to blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


# ─── messages[] → input[] ────────────────────────────────────────────────────

def _translate_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate Anthropic messages into Responses ``input`` items.

    A single Anthropic message can carry multiple content blocks that map to
    *different* Responses item types (e.g. an assistant turn with a thinking
    block + text + tool_use). We flatten in order. No item ever carries ``id``
    or ``status`` (surprise #6)."""
    input_items: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        # The Codex backend 400s input items with role "system" ("System messages
        # are not allowed") but accepts "developer" (live-verified 2026-07-19); the
        # CLI's "Available agent types" context arrives as a role:"system" message,
        # so remap system/developer → developer here.
        out_role = "developer" if role in ("system", "developer") else role
        blocks = _normalize_message_content(message.get("content"))

        # Text blocks for this message are coalesced into one message item so we
        # do not fragment a plain turn into many items.
        text_segments: List[str] = []

        def _flush_text() -> None:
            if not text_segments:
                return
            joined = "".join(text_segments)
            content_type = "output_text" if out_role == "assistant" else "input_text"
            input_items.append(
                {
                    "type": "message",
                    "role": out_role,
                    "content": [{"type": content_type, "text": joined}],
                }
            )
            text_segments.clear()

        for block in blocks:
            btype = block.get("type")

            if btype == "text":
                text_segments.append(str(block.get("text", "")))

            elif btype == "tool_use":
                # Assistant tool call → Responses function_call.
                _flush_text()
                raw_input = block.get("input", {})
                # arguments must be a JSON *string*; deterministic serialization.
                arguments = json.dumps(
                    raw_input, separators=(",", ":"), ensure_ascii=False
                )
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": arguments,
                    }
                )

            elif btype == "tool_result":
                # User-provided tool result → Responses function_call_output,
                # joined back to the call by call_id.
                _flush_text()
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.get("tool_use_id"),
                        "output": _stringify_tool_result_content(block.get("content")),
                    }
                )

            elif btype == "thinking":
                # Reverse the §D4 signature envelope. A valid envelope → a real
                # reasoning input item (encrypted_content preserved verbatim). A
                # foreign signature (native-Claude thinking) → DROP; we never
                # fabricate a reasoning item the upstream did not produce.
                _flush_text()
                envelope = decode_reasoning_signature(block.get("signature"))
                if envelope is None:
                    continue  # drop native-Claude / unrecognized thinking
                input_items.append(
                    {
                        "type": "reasoning",
                        "content": envelope.get("content", []),
                        "encrypted_content": envelope.get("encrypted_content"),
                        "summary": envelope.get("summary", []),
                    }
                )

            elif btype == "redacted_thinking":
                # Anthropic-encrypted native reasoning — not ours, cannot map to
                # a Responses reasoning item; drop (same rule as foreign thinking).
                _flush_text()
                continue

            # Unknown block types (images, documents, …) are out of scope for the
            # stage-1 translation core and are skipped.

        _flush_text()

    return input_items


# ─── tools[] / tool_choice ───────────────────────────────────────────────────

def _translate_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        out.append(
            {
                "type": "function",
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                # Anthropic input_schema IS a JSON Schema → Responses parameters.
                # (Backend coerces strict:true + all-props-required — surprise #4;
                # nothing to do here, callers must tolerate the echo.)
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            }
        )
    return out or None


def _translate_tool_choice(tool_choice: Any) -> Optional[Any]:
    """auto/any/tool/none → Responses tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        # Already a bare string (rare); pass through known values.
        return tool_choice
    if not isinstance(tool_choice, dict):
        return None
    kind = tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "name": tool_choice.get("name")}
    return "auto"


def _parallel_tool_calls(tools: Optional[List[Any]], tool_choice: Any) -> bool:
    """§D2: parallel_tool_calls false unless the Anthropic request allows it.

    Anthropic permits parallel tool use by default and opts out via
    ``tool_choice.disable_parallel_tool_use``. We only enable parallel calls when
    tools are actually present AND parallel use is not disabled."""
    if not tools:
        return False
    if isinstance(tool_choice, dict) and tool_choice.get("disable_parallel_tool_use"):
        return False
    return True


# ─── top-level request translation ───────────────────────────────────────────

def translate_request(
    anthropic_body: Dict[str, Any],
    *,
    upstream_mode: str = "codex",
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Translate an Anthropic Messages request body into a Responses request body.

    Parameters
    ----------
    anthropic_body:
        The Anthropic ``/v1/messages`` request (``system``, ``messages``,
        ``tools``, ``tool_choice``, ``thinking``, ``max_tokens``, ``temperature``,
        ``top_p``, ``stream`` …). Not mutated.
    upstream_mode:
        ``"codex"`` (default) targets chatgpt.com/backend-api/codex and OMITS
        ``max_output_tokens`` (surprise #7). ``"openai"`` targets api.openai.com
        and forwards ``max_tokens → max_output_tokens`` when present.
    target_model:
        The real upstream model id from the routing table (stage 2). When absent,
        the Anthropic request's own ``model`` is passed through. We never forward
        a Claude alias as the upstream model when a target is supplied (§A.2).
    """
    body: Dict[str, Any] = {
        "model": target_model or anthropic_body.get("model"),
        "input": _translate_messages(anthropic_body.get("messages", []) or []),
        # Non-negotiable spike invariants.
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }

    instructions = _system_to_instructions(anthropic_body.get("system"))
    if instructions is not None:
        body["instructions"] = instructions

    tools = _translate_tools(anthropic_body.get("tools"))
    if tools is not None:
        body["tools"] = tools

    tool_choice = _translate_tool_choice(anthropic_body.get("tool_choice"))
    if tool_choice is not None:
        body["tool_choice"] = tool_choice

    body["parallel_tool_calls"] = _parallel_tool_calls(
        anthropic_body.get("tools"), anthropic_body.get("tool_choice")
    )

    # thinking → reasoning {effort, summary:"auto"}. Only when enabled; the
    # backend normalizes summary/mode/context server-side (surprise #5).
    thinking = anthropic_body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        body["reasoning"] = {
            "effort": _effort_from_budget(thinking.get("budget_tokens")),
            "summary": "auto",
        }
    elif isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        # Adaptive thinking (CLI 2.1.214) has no budget_tokens; the requested
        # effort lives in the new top-level output_config.effort.
        body["reasoning"] = {
            "effort": _normalize_effort(
                anthropic_body.get("output_config", {}).get("effort")
            ),
            "summary": "auto",
        }

    # Sampling passthrough (only if the caller set them).
    if anthropic_body.get("temperature") is not None:
        body["temperature"] = anthropic_body["temperature"]
    if anthropic_body.get("top_p") is not None:
        body["top_p"] = anthropic_body["top_p"]

    # max_output_tokens: omit for Codex (surprise #7), forward for api.openai.com.
    if upstream_mode == "openai" and anthropic_body.get("max_tokens") is not None:
        body["max_output_tokens"] = anthropic_body["max_tokens"]

    return body
