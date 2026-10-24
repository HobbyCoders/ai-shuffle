"""Keep model-authored prose from impersonating host delivery frames.

Applied to completed responses and again on replay for pre-fix transcripts.
Mentions (including code examples) are retained, but rendered as quotations.
Signed/opaque reasoning and structured tool inputs are never rewritten.
"""
from __future__ import annotations

from dataclasses import replace
import re

from fluid.messages import Message, Text, Thinking

_MARKER = re.compile(
    r"\[(/?)(agent[ •]message|user[ •]interjection|harness[ •]notice)"
    r"|<(\s*/?\s*)system-reminder\s*>", re.IGNORECASE,
)
_ROLE = re.compile(
    r"<\|(?:im_start|im_end|im_sep|meta_sep|start|end|message|"
    r"start_header_id|end_header_id|eot_id|endoftext)\|>"
    r"|\[(?:im_start|im_end|im_sep|meta_sep|start_header_id|end_header_id|eot_id)\]",
    re.IGNORECASE,
)
_PROVENANCE = re.compile(r"host-verified", re.IGNORECASE)


def quote_assistant_text(text: str) -> str:
    """No raw control tokens survive, even when mentioned inside a code fence."""
    text = _MARKER.sub(lambda m: (
        "[quoted " + (m[1] or "") + m[2].replace("•", " ")
        if m[2] else "<quoted system-reminder>"
    ), text)
    text = _ROLE.sub(lambda m: "[quoted token: " + m[0].strip("<|>[]") + "]", text)
    return _PROVENANCE.sub("quoted host verification", text)


def sanitize_assistant_history(history: list[Message]) -> list[Message]:
    """Pure: preserve actual host deliveries on non-assistant roles verbatim."""
    result = []
    for message in history:
        content = []
        changed = False
        for block in message.content:
            if message.role == "assistant" and isinstance(block, (Text, Thinking)):
                text = quote_assistant_text(block.text)
                if text != block.text:
                    block = replace(block, text=text)
                    changed = True
            content.append(block)
        result.append(replace(message, content=content) if changed else message)
    return result
