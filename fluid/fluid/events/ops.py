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
    # Internal durable-delivery marker for an automatic background wake. Hosts
    # normally leave this empty and submit BackgroundTaskFinished instead.
    wake_task_ids: tuple[str, ...] = ()


@dataclass(kw_only=True)
class Resume(Op):
    """Run the agent loop again from the session head without appending a
    user node. Valid only when the head is a user-role node the model has
    not answered yet (a text prompt or tool results); otherwise the engine
    ends the turn with an ErrorEvent saying there is nothing to resume."""


@dataclass(kw_only=True)
class Interrupt(Op):
    pass


@dataclass(kw_only=True)
class Steer(Op):
    """A mid-turn user message, delivered without interrupting the turn.

    Role alternation means mid-turn user text can only ride a tool result;
    the engine appends it to the NEXT tool result inside a bounded,
    self-describing marker the base prompt teaches the model to trust (and to
    treat as historical once an assistant message follows it). Steers queued
    when the turn ends without another tool call are dropped with a warning —
    the user can simply send a normal message then.

    ``id`` is an optional caller-chosen correlation handle. A host holding the
    message in a durable queue sets one and waits for it to come back on a
    ``SteerDelivered`` event before retiring the queued copy; if the turn ends
    first the id never appears and the host re-sends the text as a normal turn.

    ``origin`` picks the marker the text is delivered in, and with it the
    standing the model gives it:

    * ``"user"`` (the default, and what every existing caller gets) — the user's
      own words, in the steering marker that carries their authority.
    * ``"system"`` — an automated harness notice the host is routing through
      this channel: a background task finishing, a watcher firing. It arrives in
      the harness-notice marker (:func:`fluid.context.steering.format_system_notice`)
      which says plainly that it is not the user and is not asking for anything,
      so machine-generated text — including a summary some sub-agent wrote —
      can never borrow the user's authority.

    ``"peer"`` is not set here: a peer's message is an ``AgentMessage``, which
    carries the sender's name and renders in its own marker.
    """

    text: str
    id: str | None = None
    origin: str = "user"  # "user" | "system"


@dataclass(kw_only=True)
class AgentMessage(Op):
    """A message from one agent to another, delivered without interrupting it.

    Steer's sibling for peer traffic, and deliberately not Steer itself: the
    steering marker carries the user's authority (see
    :mod:`fluid.context.peer`). Delivery adapts to what the receiver is doing —
    queued for its next tool result while a turn runs, started as an ordinary
    user-role turn when it is idle — so a sender never has to race the
    receiver's state.

    ``sender`` is the name the receiver sees in the marker. ``id`` is the same
    optional correlation handle Steer takes; a mid-turn delivery receipts on
    ``SteerDelivered`` alongside the user's own interjections.
    """

    text: str
    sender: str
    id: str | None = None


@dataclass(kw_only=True)
class BackgroundTaskFinished(Op):
    """Record a detached worker's terminal result and optionally wake its parent."""

    task_id: str
    status: str = "finished"  # "finished" | "failed" | "cancelled"
    summary: str | None = None
    worker_session_id: str | None = None
    wake: bool = True
    # Where the worker's full report was written, when the host wrote one. A
    # synchronous Task hands the model the report itself; a background one has
    # to hand it a way back to the report, or the wake notification's elided
    # summary costs HistoryExpand round-trips to recover something that was
    # never in history to begin with.
    report_path: str | None = None


@dataclass(kw_only=True)
class RespondPermission(Op):
    request_id: str
    decision: str  # "allow" | "deny" | "allow_always"
    remember: bool = False
    # Why the call was denied, in the model's words. Engines surface it as the
    # denied tool's ``tool_result`` text; ``None`` keeps their generic
    # "permission denied". Lets a caller distinguish outcomes that are not
    # really permission failures (e.g. the user closing a question dialog).
    message: str | None = None
    # The (possibly rewritten) tool input to run on an allow — the SDK's
    # ``updatedInput``. ``None`` keeps the input the engine already has. This is
    # how a decider hands data back to the tool (e.g. AskUserQuestion answers).
    updated_input: dict[str, Any] | None = None


@dataclass(kw_only=True)
class RespondQuestion(Op):
    """Answer a parked ``QuestionRequest``.

    ``user_answered`` is the host's word that a PERSON produced ``answer`` —
    clicked the approval, typed the reply — as opposed to a policy (an
    auto-approve mode, an unattended runner) answering on their behalf. It
    rides into the tool's ``__input_responses__`` under
    :data:`fluid.tools.builtins.plan.USER_ANSWERED_KEY`, which is how
    StopPlanning says "approved by the user" only when one did. Unset means
    unattributed, never "approved by someone".
    """

    request_id: str
    answer: str
    user_answered: bool = False


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
