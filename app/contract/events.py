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
    terminal_reason: str | None = None
    usage: dict = Field(default_factory=dict)


class TurnUsage(EventBase):
    type: Literal["turn.usage"] = "turn.usage"
    usage: dict = Field(default_factory=dict)


class TurnAborted(EventBase):
    type: Literal["turn.aborted"] = "turn.aborted"
    reason: str | None = None
    terminal_reason: str | None = None


class MessageUser(EventBase):
    type: Literal["message.user"] = "message.user"
    content: str
    attachments: list[str] = Field(default_factory=list)  # artifact hashes
    kind: str | None = None  # e.g. 'text' | 'command'
    command_name: str | None = None
    command_args: str | None = None
    profile_id: str | None = None
    uuid: str | None = None  # raw jsonl entry uuid (rewind target)
    timestamp: datetime | None = None  # preferred over created_at when stamping


class MessageAssistant(EventBase):
    type: Literal["message.assistant"] = "message.assistant"
    content: str
    kind: Literal["text", "thinking"] = "text"
    model: str | None = None
    profile_id: str | None = None
    uuid: str | None = None  # raw jsonl entry uuid (rewind target)
    timestamp: datetime | None = None  # preferred over created_at when stamping


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
    screenshot: str | None = None  # data URI from MCP image results (rendered inline)
    timestamp: datetime | None = None  # preferred over created_at when stamping


class ToolInputDelta(EventBase):
    type: Literal["tool.input_delta"] = "tool.input_delta"
    tool_use_id: str
    partial_json: str  # batched by the runner


class ToolResult(EventBase):
    type: Literal["tool.result"] = "tool.result"
    tool_use_id: str
    is_error: bool = False
    content_preview: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None  # preferred over created_at when stamping


class SubagentStarted(EventBase):
    type: Literal["subagent.started"] = "subagent.started"
    task_id: str  # the Task/Agent tool_use_id — join key to the tool card
    agent_id: str | None = None
    agent_type: str | None = None
    description: str | None = None
    prompt_preview: str | None = None
    # The model's launch tool_use_id when it differs from task_id (omni-leg
    # Task workers key task_id on the worker session id) — lets the reducer
    # absorb the launch tool_use placeholder into this card.
    launch_tool_use_id: str | None = None


class SubagentProgress(EventBase):
    type: Literal["subagent.progress"] = "subagent.progress"
    task_id: str
    kind: Literal["chunk", "tool_use", "tool_result"] | None = None
    payload: dict = Field(default_factory=dict)
    note: str | None = None
    tokens: int | None = None


class SubagentCompleted(EventBase):
    type: Literal["subagent.completed"] = "subagent.completed"
    task_id: str
    status: Literal["completed", "failed", "killed"]
    result_preview: str | None = None
    usage: dict | None = None


class TeamMemberStarted(EventBase):
    type: Literal["team.member_started"] = "team.member_started"
    team_name: str
    teammate_name: str
    description: str | None = None
    prompt: str | None = None


class TeamMemberProgress(EventBase):
    type: Literal["team.member_progress"] = "team.member_progress"
    team_name: str
    teammate_name: str
    kind: Literal["chunk", "tool_use", "tool_result"] | None = None
    payload: dict = Field(default_factory=dict)
    note: str | None = None
    tokens: int | None = None


class TeamMemberCompleted(EventBase):
    type: Literal["team.member_completed"] = "team.member_completed"
    team_name: str
    teammate_name: str
    status: Literal["completed", "failed", "killed"]
    result_preview: str | None = None
    usage: dict | None = None


class TaskUpdated(EventBase):
    type: Literal["task.updated"] = "task.updated"
    task_id: str
    state: str
    description: str | None = None
    summary: str | None = None
    tool_use_id: str | None = None


class SystemNote(EventBase):
    type: Literal["system.note"] = "system.note"
    subtype: str
    data: dict = Field(default_factory=dict)
    timestamp: datetime | None = None  # preferred over created_at when stamping


class SystemCompactionBoundary(EventBase):
    type: Literal["system.compaction_boundary"] = "system.compaction_boundary"


class InteractionRequested(EventBase):
    type: Literal["interaction.requested"] = "interaction.requested"
    interaction_id: str
    request_id: str | None = None
    kind: Literal["permission", "question"]
    request: dict = Field(default_factory=dict)


class InteractionAnswered(EventBase):
    type: Literal["interaction.answered"] = "interaction.answered"
    interaction_id: str
    request_id: str | None = None
    answer: dict = Field(default_factory=dict)


class InteractionExpired(EventBase):
    type: Literal["interaction.expired"] = "interaction.expired"
    interaction_id: str
    request_id: str | None = None
    reason: str | None = None


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
        TurnUsage,
        MessageUser,
        MessageAssistant,
        AssistantTextDelta,
        AssistantThinkingDelta,
        ToolUse,
        ToolInputDelta,
        ToolResult,
        SubagentStarted,
        SubagentProgress,
        SubagentCompleted,
        TeamMemberStarted,
        TeamMemberProgress,
        TeamMemberCompleted,
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
    "turn.usage",
    "message.user",
    "message.assistant",
    "assistant.text_delta",
    "assistant.thinking_delta",
    "tool.use",
    "tool.input_delta",
    "tool.result",
    "subagent.started",
    "subagent.progress",
    "subagent.completed",
    "team.member_started",
    "team.member_progress",
    "team.member_completed",
    "task.updated",
    "system.note",
    "system.compaction_boundary",
    "interaction.requested",
    "interaction.answered",
    "interaction.expired",
    "transcript.rewound",
    "artifact.created",
)
