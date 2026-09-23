"""Normalized conversation content model.

Provider serializers translate these blocks to/from each API's wire format.
The one non-obvious block is ``Reasoning``: provider reasoning state
(Anthropic signed thinking, OpenAI encrypted reasoning items, Gemini thought
signatures) is model-bound and MUST round-trip byte-for-byte to the provider
that produced it — and be dropped (or reduced to plain summary text) when the
next call targets a different provider. It is never synthesized or edited.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
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


@dataclass(frozen=True)
class SteerDelivery:
    """Engine-authored delivery metadata, never inferred from tool text."""

    text: str
    origin: str = "user"
    sender: str | None = None
    id: str | None = None

    def render(self) -> str:
        from fluid.context.peer import format_peer_message
        from fluid.context.steering import format_steer, format_system_notice

        if self.origin == "system":
            return format_system_notice(self.text)
        if self.sender is not None:
            return format_peer_message(self.sender, self.text)
        return format_steer(self.text)


@dataclass
class ToolResultBlock(ContentBlock):
    tool_use_id: str
    content: list[ContentBlock]
    is_error: bool = False
    delivered_steers: list[SteerDelivery] = field(default_factory=list)
    # Legacy results cannot establish that every interjection was captured.
    delivery_metadata_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(),
                "delivered_steers": [dict(s.__dict__) for s in self.delivered_steers]}


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
    # Model that produced the envelope. Blobs and signatures are model-bound,
    # not just provider-bound: xAI and OpenAI share the openai_responses
    # dialect, and a blob resent to a model that cannot decrypt it 400s the
    # whole request. None on histories written before this field existed.
    model: str | None = None


@dataclass
class UnknownBlock(ContentBlock):
    """A stored block whose ``kind`` this release does not know.

    Workers update one at a time, so a session written by a newer release can
    be resumed by an older one. The block keeps the stored dict in ``raw``
    (``kind`` included) and serializes back to exactly that dict, so a load
    and save loses nothing. Every provider serializer and renderer skips it:
    it puts nothing in the model's view.
    """

    kind: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


_KINDS = {
    "text": Text,
    "thinking": Thinking,
    "tooluseblock": ToolUseBlock,
    "toolresultblock": ToolResultBlock,
    "image": Image,
    "reasoning": Reasoning,
}


def _known_fields(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """``d`` without the keys ``cls`` has no field for.

    A newer release may add a field to a known block; this release drops it
    on load rather than failing to construct the block.
    """
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def block_from_dict(d: dict[str, Any]) -> ContentBlock:
    kind = d.get("kind")
    cls = _KINDS.get(kind) if isinstance(kind, str) else None
    if cls is None:
        return UnknownBlock(
            kind=kind if isinstance(kind, str) else "", raw=copy.deepcopy(dict(d))
        )
    d = _known_fields(cls, d)
    if cls is ToolResultBlock:
        d["content"] = [block_from_dict(c) for c in d.get("content", [])]
        d["delivered_steers"] = [
            SteerDelivery(**_known_fields(SteerDelivery, s))
            for s in d.get("delivered_steers", [])
        ]
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
