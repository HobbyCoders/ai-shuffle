"""Normalized conversation content model.

Provider serializers translate these blocks to/from each API's wire format.
The one non-obvious block is ``Reasoning``: provider reasoning state
(Anthropic signed thinking, OpenAI encrypted reasoning items, Gemini thought
signatures) is model-bound and MUST round-trip byte-for-byte to the provider
that produced it — and be dropped (or reduced to plain summary text) when the
next call targets a different provider. It is never synthesized or edited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContentBlock:
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.__class__.__name__.lower()}
        d.update(self.__dict__)
        return d


@dataclass
class Text(ContentBlock):
    text: str


@dataclass
class Thinking(ContentBlock):
    """Human-visible thinking text (streamed). Signature lives in Reasoning."""

    text: str


@dataclass
class ToolUseBlock(ContentBlock):
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock(ContentBlock):
    tool_use_id: str
    content: list[ContentBlock]
    is_error: bool = False


@dataclass
class Image(ContentBlock):
    media_type: str
    data: str  # base64


@dataclass
class Reasoning(ContentBlock):
    """Provider-opaque reasoning envelope. Round-tripped verbatim; dropped on
    model/provider switch."""

    origin: str  # "anthropic" | "openai_responses" | ...
    summary_text: str | None = None
    signature: str | None = None  # Anthropic signed thinking
    encrypted_content: str | None = None  # OpenAI encrypted reasoning item
    raw: dict[str, Any] | None = None  # exact item for verbatim resend


_KINDS = {
    "text": Text,
    "thinking": Thinking,
    "tooluseblock": ToolUseBlock,
    "toolresultblock": ToolResultBlock,
    "image": Image,
    "reasoning": Reasoning,
}


def block_from_dict(d: dict[str, Any]) -> ContentBlock:
    d = dict(d)
    cls = _KINDS[d.pop("kind")]
    if cls is ToolResultBlock:
        d["content"] = [block_from_dict(c) for c in d.get("content", [])]
    return cls(**d)


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: list[ContentBlock]
    provider_meta: dict[str, Any] = field(default_factory=dict)  # previous_response_id, model, ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": [
                {**b.to_dict(), "content": [c.to_dict() for c in b.content]}
                if isinstance(b, ToolResultBlock)
                else b.to_dict()
                for b in self.content
            ],
            "provider_meta": self.provider_meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"],
            content=[block_from_dict(b) for b in d["content"]],
            provider_meta=d.get("provider_meta", {}),
        )

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, Text))
