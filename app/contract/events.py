"""V2 session event models (discriminated union).

Single source of truth for the typed event stream. TS types are generated from
the JSON schema exported here — see app/contract/export.py.
"""

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class EventBase(BaseModel):
    id: str  # server-assigned uuid4
    session_id: str
    seq: int  # per-session monotonic, server-assigned
    run_id: str | None = None  # fenced lease that produced it
    turn_id: str | None = None
    command_id: str | None = None  # client command echo
    created_at: datetime


class TurnQueued(EventBase):
    type: Literal["turn.queued"] = "turn.queued"
    prompt_preview: str | None = None


class TurnStarted(EventBase):
    type: Literal["turn.started"] = "turn.started"


class TurnInterrupting(EventBase):
    type: Literal["turn.interrupting"] = "turn.interrupting"
    mode: Literal["stop", "send_now"]


class TurnParked(EventBase):
    type: Literal["turn.parked"] = "turn.parked"
    task_ids: list[str] = Field(default_factory=list)


class TurnCompleted(EventBase):
    type: Literal["turn.completed"] = "turn.completed"
    stop_reason: str | None = None


class TurnAborted(EventBase):
    type: Literal["turn.aborted"] = "turn.aborted"
    reason: str | None = None


class MessageUser(EventBase):
    type: Literal["message.user"] = "message.user"
    content: str
    attachments: list[str] = Field(default_factory=list)  # artifact hashes


class AssistantTextDelta(EventBase):
    type: Literal["assistant.text_delta"] = "assistant.text_delta"
    text: str  # batched by the runner


class AssistantThinkingDelta(EventBase):
    type: Literal["assistant.thinking_delta"] = "assistant.thinking_delta"
    text: str


class ToolUse(EventBase):
    type: Literal["tool.use"] = "tool.use"
    tool_use_id: str
    name: str
    input: dict = Field(default_factory=dict)


class ToolResult(EventBase):
    type: Literal["tool.result"] = "tool.result"
    tool_use_id: str
    is_error: bool = False
    content_preview: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)


class SubagentStarted(EventBase):
    type: Literal["subagent.started"] = "subagent.started"
    task_id: str
    description: str | None = None


class SubagentProgress(EventBase):
    type: Literal["subagent.progress"] = "subagent.progress"
    task_id: str
    note: str | None = None
    tokens: int | None = None


class SubagentCompleted(EventBase):
    type: Literal["subagent.completed"] = "subagent.completed"
    task_id: str
    status: Literal["completed", "failed", "killed"]


class TaskUpdated(EventBase):
    type: Literal["task.updated"] = "task.updated"
    task_id: str
    state: str


class SystemNote(EventBase):
    type: Literal["system.note"] = "system.note"
    subtype: str
    data: dict = Field(default_factory=dict)


class SystemCompactionBoundary(EventBase):
    type: Literal["system.compaction_boundary"] = "system.compaction_boundary"


class InteractionRequested(EventBase):
    type: Literal["interaction.requested"] = "interaction.requested"
    interaction_id: str
    kind: Literal["permission", "question"]
    request: dict = Field(default_factory=dict)


class InteractionAnswered(EventBase):
    type: Literal["interaction.answered"] = "interaction.answered"
    interaction_id: str
    answer: dict = Field(default_factory=dict)


class InteractionExpired(EventBase):
    type: Literal["interaction.expired"] = "interaction.expired"
    interaction_id: str


class TranscriptRewound(EventBase):
    type: Literal["transcript.rewound"] = "transcript.rewound"
    from_seq: int


class ArtifactCreated(EventBase):
    type: Literal["artifact.created"] = "artifact.created"
    hash: str
    mime: str | None = None
    filename: str | None = None
    size: int


SessionEvent = Annotated[
    Union[
        TurnQueued,
        TurnStarted,
        TurnInterrupting,
        TurnParked,
        TurnCompleted,
        TurnAborted,
        MessageUser,
        AssistantTextDelta,
        AssistantThinkingDelta,
        ToolUse,
        ToolResult,
        SubagentStarted,
        SubagentProgress,
        SubagentCompleted,
        TaskUpdated,
        SystemNote,
        SystemCompactionBoundary,
        InteractionRequested,
        InteractionAnswered,
        InteractionExpired,
        TranscriptRewound,
        ArtifactCreated,
    ],
    Field(discriminator="type"),
]

EVENT_TYPES: tuple[str, ...] = (
    "turn.queued",
    "turn.started",
    "turn.interrupting",
    "turn.parked",
    "turn.completed",
    "turn.aborted",
    "message.user",
    "assistant.text_delta",
    "assistant.thinking_delta",
    "tool.use",
    "tool.result",
    "subagent.started",
    "subagent.progress",
    "subagent.completed",
    "task.updated",
    "system.note",
    "system.compaction_boundary",
    "interaction.requested",
    "interaction.answered",
    "interaction.expired",
    "transcript.rewound",
    "artifact.created",
)
