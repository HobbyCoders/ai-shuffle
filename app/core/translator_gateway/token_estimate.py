"""
Deterministic token estimator for ``POST /v1/messages/count_tokens`` (§A.3 / §D5).

Claude Code preflights a token count before every request; if that count is wildly
wrong it misroutes auto-compaction (the LiteLLM #30043 failure class). We do NOT
have the upstream provider's exact tokenizer here, so this is a deterministic
approximation: ~4 characters per token plus fixed per-message / per-tool overheads.

Guarantees (not precision):
  * DETERMINISTIC — same request body → same count, always.
  * MONOTONIC — adding any text/message/tool never decreases the count.

Stage 2 wires this to the count_tokens endpoint. No I/O, no network.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List

# ── Estimation constants (documented; tune, don't randomize) ─────────────────
_CHARS_PER_TOKEN = 4          # classic BPE rule-of-thumb
_BASE_OVERHEAD = 3            # fixed request framing (BOS / role scaffolding)
_PER_MESSAGE_OVERHEAD = 4     # per message: role + delimiters
_PER_TOOL_OVERHEAD = 8        # per tool definition: schema framing
_PER_SYSTEM_OVERHEAD = 3      # system prompt framing when present


def _chars_to_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return math.ceil(chars / _CHARS_PER_TOKEN)


def _text_of_content(content: Any) -> str:
    """Flatten any Anthropic content shape (string or list of blocks) into a
    single string whose *length* drives the estimate. All text-bearing fields
    contribute; unknown blocks are JSON-encoded so their size still counts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "thinking":
                parts.append(str(block.get("thinking", "")))
                parts.append(str(block.get("signature", "")))
            elif btype == "tool_use":
                parts.append(str(block.get("name", "")))
                parts.append(
                    json.dumps(block.get("input", {}), separators=(",", ":"), ensure_ascii=False)
                )
            elif btype == "tool_result":
                parts.append(_text_of_content(block.get("content")))
            else:
                parts.append(json.dumps(block, separators=(",", ":"), ensure_ascii=False))
        return "".join(parts)
    return json.dumps(content, separators=(",", ":"), ensure_ascii=False)


def count_tokens(anthropic_body: Dict[str, Any]) -> int:
    """Estimate the token count of an Anthropic Messages request body.

    Returns a non-negative integer. Deterministic and monotonic (see module
    docstring)."""
    total_chars = 0
    total = _BASE_OVERHEAD

    # system
    system = anthropic_body.get("system")
    if system:
        total_chars += len(_text_of_content(system))
        total += _PER_SYSTEM_OVERHEAD

    # messages
    messages = anthropic_body.get("messages") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += _PER_MESSAGE_OVERHEAD
        total_chars += len(str(message.get("role", "")))
        total_chars += len(_text_of_content(message.get("content")))

    # tools
    tools = anthropic_body.get("tools") or []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        total += _PER_TOOL_OVERHEAD
        total_chars += len(str(tool.get("name", "")))
        total_chars += len(str(tool.get("description", "")))
        total_chars += len(
            json.dumps(tool.get("input_schema", {}), separators=(",", ":"), ensure_ascii=False)
        )

    total += _chars_to_tokens(total_chars)
    return total
