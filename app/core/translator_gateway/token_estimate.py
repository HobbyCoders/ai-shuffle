"""
Token estimator for ``POST /v1/messages/count_tokens`` (§A.3 / §D5).

Claude Code preflights a token count before every request; if that count is wildly
wrong it misroutes auto-compaction and the prompt-too-long preflight (the LiteLLM
#30043 failure class). Code/JSON tokenizes far hotter than the classic ~4 chars/token
rule-of-thumb, so a heuristic under-count let real context overflow the upstream
window before compaction fired — a real "Prompt is too long" production failure.

We therefore count with a real BPE tokenizer (tiktoken ``o200k_base``, the encoding
used by current GPT/codex models) when it is available, and fall back to the
~4-chars/token heuristic on ANY failure. The fallback matters because tiktoken
fetches its BPE vocabulary file on first use, and that fetch can fail in an offline
container (no network, cold cache); the failure is cached so we do not re-attempt it
on every call. The heuristic path is otherwise identical to the pre-change behavior.

Guarantees:
  * DETERMINISTIC — same request body → same count, always (both paths).
  * MONOTONIC — adding any text/message/tool never decreases the count.

No mandatory I/O or network on the hot path: the encoder is initialized lazily and
cached (including a cached failure), and counting itself never touches the network.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

# ── Estimation constants (documented; tune, don't randomize) ─────────────────
_CHARS_PER_TOKEN = 4          # classic BPE rule-of-thumb (heuristic fallback)
_BASE_OVERHEAD = 3            # fixed request framing (BOS / role scaffolding)
_PER_MESSAGE_OVERHEAD = 4     # per message: role + delimiters
_PER_TOOL_OVERHEAD = 8        # per tool definition: schema framing
_PER_SYSTEM_OVERHEAD = 3      # system prompt framing when present

# Lazily-initialized, module-cached tiktoken encoder. `_ENCODER_INIT_DONE`
# guards against re-attempting a failed import/BPE-fetch on every call — a
# failure caches `_ENCODER = None` so the heuristic is used from then on.
_ENCODER: Optional[Any] = None
_ENCODER_INIT_DONE = False


def _get_encoder() -> Optional[Any]:
    """Return a cached tiktoken ``o200k_base`` encoder, or ``None`` if it could
    not be initialized. Attempted at most once per process: ``import tiktoken``
    and the first ``get_encoding`` call fetch the BPE vocabulary and may fail in
    an offline container, so any Exception caches a ``None`` fallback."""
    global _ENCODER, _ENCODER_INIT_DONE
    if _ENCODER_INIT_DONE:
        return _ENCODER
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("o200k_base")
    except Exception:
        _ENCODER = None
    finally:
        _ENCODER_INIT_DONE = True
    return _ENCODER


def _count_text_tokens(text: str) -> int:
    """Token count for a single string: real BPE via tiktoken when available,
    else the ~4-chars/token heuristic. The encode call is itself guarded so a
    per-string encode failure falls back to the heuristic for that string only."""
    if not text:
        return 0
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return _chars_to_tokens(len(text))


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
    docstring). Counts each text string with tiktoken when available, else the
    ~4-chars/token heuristic (see module docstring for the fallback rationale)."""
    total = _BASE_OVERHEAD

    # When the encoder is unavailable we preserve the pre-change heuristic
    # exactly: sum all character lengths, then take a single ceil at the end.
    # With the encoder we accumulate real BPE token counts per string. The
    # body-walking structure (which strings get counted) is identical either way.
    heuristic = _get_encoder() is None
    total_chars = 0
    total_text_tokens = 0

    def _add(text: str) -> None:
        nonlocal total_chars, total_text_tokens
        if heuristic:
            total_chars += len(text)
        else:
            total_text_tokens += _count_text_tokens(text)

    # system
    system = anthropic_body.get("system")
    if system:
        _add(_text_of_content(system))
        total += _PER_SYSTEM_OVERHEAD

    # messages
    messages = anthropic_body.get("messages") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += _PER_MESSAGE_OVERHEAD
        _add(str(message.get("role", "")))
        _add(_text_of_content(message.get("content")))

    # tools
    tools = anthropic_body.get("tools") or []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        total += _PER_TOOL_OVERHEAD
        _add(str(tool.get("name", "")))
        _add(str(tool.get("description", "")))
        _add(
            json.dumps(tool.get("input_schema", {}), separators=(",", ":"), ensure_ascii=False)
        )

    total += total_text_tokens if not heuristic else _chars_to_tokens(total_chars)
    return total
