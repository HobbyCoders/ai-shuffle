"""Ops — the input side of the engine.

Clients submit Ops; the engine emits Events. This is the Codex-style
submit/event split: the caller never awaits a "response," it observes the
event stream.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def _op_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(kw_only=True)
class Op:
    op_id: str = field(default_factory=_op_id)


@dataclass(kw_only=True)
class UserTurn(Op):
    """Start a turn. ``parent_node_id`` selects the fork point (None = session head)."""

    content: list[Any]  # list[ContentBlock] | str convenience handled by the engine
    parent_node_id: str | None = None


@dataclass(kw_only=True)
class Interrupt(Op):
    pass


@dataclass(kw_only=True)
class RespondPermission(Op):
    request_id: str
    decision: str  # "allow" | "deny" | "allow_always"
    remember: bool = False


@dataclass(kw_only=True)
class RespondQuestion(Op):
    request_id: str
    answer: str


@dataclass(kw_only=True)
class Handoff(Op):
    """Start a fresh session seeded with a model-written handoff document
    (Amp-style): structured goal/state/next-steps instead of cumulative
    summarization. The engine emits ``HandoffCreated`` with the new session id.
    """

    goal: str | None = None


@dataclass(kw_only=True)
class SetModel(Op):
    model: str


@dataclass(kw_only=True)
class SetPermissionMode(Op):
    mode: str


@dataclass(kw_only=True)
class Fork(Op):
    from_node_id: str


@dataclass(kw_only=True)
class Rewind(Op):
    to_node_id: str
