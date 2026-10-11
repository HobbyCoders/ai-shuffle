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


class QueuedMessageWithdrawn(EventBase):
    """Content-free receipt for a held message withdrawn before it became a turn."""

    type: Literal["queued_message.withdrawn"] = "queued_message.withdrawn"
    message_id: str


class TurnStarted(EventBase):
    type: Literal["turn.started"] = "turn.started"
    profile_id: str | None = None
    # What started the turn, in fluid's vocabulary (``TurnStarted.source``:
    # user, agent_message, wakeup, unpark, promotion, automation). A failover
    # retry carries the source of the turn it retries. None when not recorded.
    source: str | None = None


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
    usage: dict = Field(
        default_factory=dict,
        description=(
            "The turn's usage summary (tokens_in, tokens_out, cache_read_tokens, "
            "cache_creation_tokens, total_cost_usd, duration_ms, num_turns). "
            "usage_estimated: true when any token count in this turn was "
            "estimated by the harness because the provider reported no usage "
            "or the reply was interrupted; absent otherwise."
        ),
    )


class TurnUsage(EventBase):
    type: Literal["turn.usage"] = "turn.usage"
    usage: dict = Field(
        default_factory=dict,
        description=(
            "One model call's usage record (input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, cost_usd, "
            "model_call_index). usage_estimated: true when a token count in "
            "this record was estimated by the harness (input_tokens_estimated / "
            "output_tokens_estimated name the guessed fields); absent otherwise."
        ),
    )
    # Context-window breakdown for the model call this usage record describes
    # (fluid's per-call snapshot). Same shape as the /context-usage fetch, so
    # the client can drive the live bar from the event stream instead of
    # polling. None whenever the engine could not take a snapshot; the payload
    # omits the key entirely in that case.
    context_usage: dict | None = None


class TurnProviderAlive(EventBase):
    """The provider accepted this turn's model request and is still streaming.

    Recorded from fluid's ``ProviderAlive``. It proves the request was accepted
    and the response stream was open at that moment. It does not prove that
    useful work continues. Liveness evidence for the Task worker's first-frame
    watchdog (#1418), never content: clients ignore it. ``signal`` is fluid's
    reason ("accepted", "reasoning", "keepalive").
    """

    type: Literal["turn.provider_alive"] = "turn.provider_alive"
    signal: str | None = None


class TurnAborted(EventBase):
    type: Literal["turn.aborted"] = "turn.aborted"
    reason: str | None = None
    terminal_reason: str | None = None


class MessageUser(EventBase):
    type: Literal["message.user"] = "message.user"
    content: str
    queued_turn_id: str | None = None
    checkpoint_backed: bool | None = None
    attachments: list[str] = Field(default_factory=list)  # artifact hashes
    kind: str | None = None  # e.g. 'text' | 'command'
    command_name: str | None = None
    command_args: str | None = None
    profile_id: str | None = None
    uuid: str | None = None  # raw jsonl entry uuid (rewind target)
    checkpoint_backed: bool | None = None  # False for mid-turn steers; None for legacy events
    timestamp: datetime | None = None  # preferred over created_at when stamping


class MessageAssistant(EventBase):
    type: Literal["message.assistant"] = "message.assistant"
    content: str
    kind: Literal["text", "thinking"] = "text"
    # Thinking only: "reasoning_summary" when the block is the provider's own
    # summary of its reasoning rather than the reasoning text (fluid
    # BlockStart.subtype). None when the provider draws no distinction.
    subtype: str | None = None
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
    subtype: str | None = None  # see MessageAssistant.subtype


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
    # Known from the block's first frame, so the card that opens on the first
    # delta can label itself instead of rendering an anonymous box. Optional:
    # a producer that does not know the name still emits valid deltas.
    name: str | None = None


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
    # App worker transcript session; native SDK agent IDs are not session IDs.
    worker_session_id: str | None = None
    agent_type: str | None = None
    # The dispatched agent's display NAME ("Frontend Designer") — the card's
    # identity. ``agent_type`` keeps carrying the model slug (unchanged for
    # compatibility), which the card renders as a secondary detail.
    agent_name: str | None = None
    description: str | None = None
    prompt_preview: str | None = None
    # The model's launch tool_use_id when it differs from task_id (fluid-leg
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
    result: str | None = None
    result_truncated: bool = False
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
    # Start of the current assignment, not the worker session's creation.
    started_at: str | None = None
    description: str | None = None
    summary: str | None = None
    tool_use_id: str | None = None
    # Registry kind ("agent" | "shell") — stamped from the task_registry at
    # translation time so the FE tail line shows the right badge instead of
    # defaulting every task to 'agent'. Optional/additive: older logs omit it.
    kind: str | None = None
    # The other worker a remote Task child runs on (its display name). Absent
    # for local tasks. Optional/additive, like ``kind``.
    worker_name: str | None = None
    # The "<provider> · <model>" label a Task worker runs on, from the
    # registry, so the running-tasks list shows each subagent's model (#1827).
    # Absent for shells and older logs. Optional/additive, like ``kind``.
    model: str | None = None


class SystemNote(EventBase):
    type: Literal["system.note"] = "system.note"
    subtype: str
    data: dict = Field(default_factory=dict)
    timestamp: datetime | None = None  # preferred over created_at when stamping


class SystemCompactionBoundary(EventBase):
    type: Literal["system.compaction_boundary"] = "system.compaction_boundary"


class SystemCompactionStatus(EventBase):
    """A compaction of the main conversation started or stopped.

    Progress, not a record: ``started`` shows "Compacting context..." until
    the ``system.compaction_boundary`` card (success) or a ``failed`` status
    (with ``error``) replaces it; ``ended`` closes it without a verdict. The
    turn's end clears it too, so a lost close can never leave it stuck.
    """

    type: Literal["system.compaction_status"] = "system.compaction_status"
    state: Literal["started", "ended", "failed"]
    trigger: str | None = None  # "auto" | "manual" when the engine knows
    requested_by: str | None = None  # "user" | "app" for a /compact turn
    error: str | None = None


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


class SwarmTask(BaseModel):
    id: str
    title: str
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["queued", "running", "completed", "failed", "blocked", "cancelled", "interrupted"]
    task_id: str | None = None
    worker_session_id: str | None = None
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SwarmSnapshot(BaseModel):
    id: str
    session_id: str
    title: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    created_at: datetime
    updated_at: datetime
    tasks: list[SwarmTask]


class SwarmUpdated(EventBase):
    type: Literal["swarm.updated"] = "swarm.updated"
    swarm: SwarmSnapshot


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
        SwarmUpdated,
        SystemNote,
        SystemCompactionBoundary,
        SystemCompactionStatus,
        InteractionRequested,
        InteractionAnswered,
        InteractionExpired,
        TranscriptRewound,
        ArtifactCreated,
        TurnProviderAlive,
        QueuedMessageWithdrawn,
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
    "swarm.updated",
    "system.note",
    "system.compaction_boundary",
    "system.compaction_status",
    "interaction.requested",
    "interaction.answered",
    "interaction.expired",
    "transcript.rewound",
    "artifact.created",
    "turn.provider_alive",
    "queued_message.withdrawn",
)
