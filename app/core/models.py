"""
Pydantic models for API requests and responses
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field


# ============================================================================
# Authentication Models
# ============================================================================

class SetupRequest(BaseModel):
    """First-run admin setup request"""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    """Login request"""
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """Admin password change request"""
    current_password: str
    new_password: str = Field(..., min_length=8)


class AuthStatus(BaseModel):
    """Authentication status response"""
    authenticated: bool
    is_admin: bool = True
    setup_required: bool
    claude_authenticated: bool
    username: Optional[str] = None


# ============================================================================
# Subagent Models
# ============================================================================

class SubagentDefinition(BaseModel):
    """Definition for a subagent that can be invoked by the main agent"""
    description: str = Field(..., min_length=1, description="When to use this agent")
    prompt: str = Field(..., min_length=1, description="System prompt for the agent")
    tools: Optional[List[str]] = Field(None, description="Allowed tools (inherits all if not specified)")
    model: Optional[str] = Field(None, description="Model override: sonnet, opus, haiku, or inherit")
    skills: Optional[List[str]] = Field(None, description="Skills the agent can use")
    memory: Optional[str] = Field(None, description="Memory scope: user, project, or local")
    mcp_servers: Optional[List[Any]] = Field(None, description="MCP servers the agent can access (names or config dicts)")


# ============================================================================
# Profile Models
# ============================================================================

# AIToolsConfig is dynamically generated from the AI_TOOLS registry
# This ensures the model always matches available tools - no manual sync needed
from app.core.ai_tools import AIToolsConfig  # noqa: E402

# Single source of truth for selectable Claude models.
from app.core.model_registry import DEFAULT_MODEL  # noqa: E402


class SystemPromptConfig(BaseModel):
    """System prompt configuration"""
    type: str = "preset"  # "preset" or "custom"
    preset: Optional[str] = "claude_code"
    content: Optional[str] = None
    append: Optional[str] = None
    inject_env_details: bool = False  # Inject environment info (working dir, platform, date, etc.)


class ComputerUseConfig(BaseModel):
    """Host desktop control (screenshots, mouse, keyboard). Native mode only.

    Off by default; the server is also gated on host availability at runtime
    (auto-disabled in Docker/headless). See app/mcp/computer_use_sdk.py.
    """
    enabled: bool = False
    target_display: Optional[int] = None  # initial monitor (mss index); None = primary
    virtual_width: Optional[int] = None   # screenshot downscale target width
    virtual_height: Optional[int] = None  # screenshot downscale target height
    require_confirmation: bool = False    # route each action through the approval UI


class ProfileConfig(BaseModel):
    """Claude Agent configuration stored in profile - maps to ClaudeAgentOptions"""
    # Core settings
    model: Optional[str] = DEFAULT_MODEL
    permission_mode: Optional[str] = "default"  # default, auto, acceptEdits, plan, bypassPermissions
    thinking_mode: Optional[str] = "default"  # default, disabled, adaptive, think, think_harder, ultra_think
    # Opus 4.6+ uses effort-based reasoning instead of token budgets
    # effort: low, medium, high, max, xhigh (xhigh only for Opus 4.7+)
    effort: Optional[str] = None  # low, medium, high, max, xhigh - for Opus 4.6+ models
    max_turns: Optional[int] = None

    # Tool configuration - SDK 0.1.12+ supports new 'tools' option
    # tools: Explicit list of tools to enable, or "claude_code" preset (new SDK way)
    # allowed_tools: Legacy - only these tools allowed (if tools not set)
    # disallowed_tools: Block these tools (can be combined with tools or allowed_tools)
    tools: Optional[List[str]] = None  # New SDK 0.1.12+ - explicit tool list
    tools_preset: Optional[str] = None  # "claude_code" to use preset (SDK 0.1.12+)
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    # UI-only: which tool-access mode the profile editor is in ("all", "none",
    # "allow", "disallow"). Persisted so the dropdown round-trips unambiguously
    # (e.g. "Allow Only Selected" with no tools yet picked can't be inferred from
    # allowed_tools alone). The runtime ignores this and reads allowed_tools /
    # disallowed_tools directly.
    tool_selection_mode: Optional[str] = None

    # AI tools configuration - individual toggles for each AI tool
    ai_tools: Optional[AIToolsConfig] = None

    # Computer-use (host desktop control). Off by default; native mode only.
    computer_use: Optional[ComputerUseConfig] = None

    # System prompt
    system_prompt: Optional[SystemPromptConfig] = None

    # Streaming behavior
    include_partial_messages: bool = False  # Enable streaming partial messages

    # Session behavior
    continue_conversation: bool = False  # Continue most recent conversation
    fork_session: bool = False  # Fork instead of continuing when resuming

    # Working directory and paths
    cwd: Optional[str] = None  # Override working directory
    add_dirs: Optional[List[str]] = None  # Additional directories Claude can access

    # Settings loading
    setting_sources: Optional[List[str]] = None  # user, project, local

    # Environment and arguments
    env: Optional[Dict[str, str]] = None  # Environment variables
    extra_args: Optional[Dict[str, Any]] = None  # Additional CLI arguments

    # Buffer settings
    max_buffer_size: Optional[int] = None  # Maximum bytes when buffering CLI stdout

    # User identification
    user: Optional[str] = None  # User identifier

    # Enabled subagent IDs - references global subagents by ID
    enabled_agents: Optional[List[str]] = None

    # Plugins - list of plugin paths to load
    # Each entry should be a path to a plugin directory containing .claude-plugin/plugin.json
    # Paths can be relative (resolved from workspace) or absolute
    enabled_plugins: Optional[List[str]] = None

    # File checkpointing for rewind (SDK 0.1.15+)
    enable_file_checkpointing: bool = False  # Enable file snapshots for rewind

    # App configuration tools - allows the agent to manage hooks, automations,
    # MCP servers, and profiles through conversation. Only effective for admin
    # sessions (never exposed to API users regardless of this setting).
    app_config_tools: bool = False

    # Lock MCP server set deterministically per session (SDK 0.1.74+).
    # When True, the SDK validates that no MCP server is added or removed
    # mid-session; prevents tool-set drift across turns.
    strict_mcp_config: bool = False



class ProfileBase(BaseModel):
    """Base profile fields"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    config: ProfileConfig


class ProfileCreate(ProfileBase):
    """Profile creation request - id is optional; if omitted, derived from name"""
    id: Optional[str] = Field(None, pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)


class ProfileUpdate(BaseModel):
    """Profile update request"""
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[ProfileConfig] = None


class Profile(ProfileBase):
    """Full profile response"""
    id: str
    is_builtin: bool = False
    is_modified: bool = False  # Whether built-in profile has been modified from defaults
    session_count: Optional[int] = None  # Number of sessions using this profile
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Project Models
# ============================================================================

class ProjectSettings(BaseModel):
    """Project-specific settings"""
    default_profile_id: Optional[str] = None
    custom_instructions: Optional[str] = None


class ProjectBase(BaseModel):
    """Base project fields"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class ProjectCreate(ProjectBase):
    """Project creation request"""
    id: str = Field(..., pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)
    path: Optional[str] = Field(
        None,
        description="Absolute filesystem path for 'add existing' projects (local mode only). "
                    "When set, the project points to this directory instead of workspace_dir/<id>."
    )
    settings: Optional[ProjectSettings] = None


class ProjectUpdate(BaseModel):
    """Project update request"""
    name: Optional[str] = None
    description: Optional[str] = None
    path: Optional[str] = None
    settings: Optional[ProjectSettings] = None


class Project(ProjectBase):
    """Full project response"""
    id: str
    path: str
    settings: ProjectSettings
    session_count: Optional[int] = None  # Number of sessions in this project
    link_group_id: Optional[str] = None  # Groups same-repo clones across installs
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Session Models
# ============================================================================

class SubagentChildMessage(BaseModel):
    """A child message within a subagent (tool call or text)"""
    id: str
    type: str  # 'text', 'tool_use', 'tool_result'
    content: str
    toolName: Optional[str] = None
    toolId: Optional[str] = None
    toolInput: Optional[Dict[str, Any]] = None
    toolResult: Optional[str] = None  # Result content for tool_use (grouped with tool call)
    toolStatus: Optional[str] = None  # 'running', 'complete', 'error'
    toolScreenshot: Optional[str] = None  # data: URI of an inline screenshot (Chrome/computer-use, Read of image)
    timestamp: Optional[str] = None


class SessionMessage(BaseModel):
    """A message in a session"""
    id: Union[int, str]  # int from DB, string from JSONL
    role: str  # user, assistant, system, tool
    content: str
    type: Optional[str] = None  # text, tool_use, tool_result, subagent, system - critical for rendering
    subtype: Optional[str] = None  # For system messages (e.g., local_command, compact_boundary)
    tool_name: Optional[str] = None  # snake_case for DB compatibility
    tool_input: Optional[Dict[str, Any]] = None  # snake_case for DB compatibility
    toolName: Optional[str] = None  # camelCase for frontend compatibility
    toolInput: Optional[Dict[str, Any]] = None  # camelCase for frontend compatibility
    toolId: Optional[str] = None  # Tool ID for matching tool_use to tool_result
    toolResult: Optional[str] = None  # Result content for tool_use (grouped with tool call)
    toolStatus: Optional[str] = None  # 'running', 'complete', 'error'
    toolScreenshot: Optional[str] = None  # data: URI of an inline screenshot (Chrome/computer-use, Read of image)
    metadata: Optional[Dict[str, Any]] = None
    # Profile that was active when this (user) message was created. Both keys are
    # emitted: snake_case for API/DB parity, camelCase for the frontend message
    # store (which reads `profileId`). Without these on the model, FastAPI's
    # response_model strips the per-message profile and the chat UI falls back to
    # the session's CURRENT profile for every bubble.
    profile_id: Optional[str] = None
    profileId: Optional[str] = None
    created_at: Optional[datetime] = None  # Optional for JSONL messages without timestamp
    # Subagent-specific fields (for type='subagent')
    agentId: Optional[str] = None  # The subagent's unique ID
    agentType: Optional[str] = None  # The subagent type (e.g., 'Explore', 'Plan')
    agentDescription: Optional[str] = None  # Task description from the Task tool
    agentPrompt: Optional[str] = None  # The initial prompt sent to the subagent
    agentStatus: Optional[str] = None  # 'pending', 'running', 'completed', 'error'
    agentChildren: Optional[List[SubagentChildMessage]] = None  # Nested messages from subagent


class SessionBase(BaseModel):
    """Base session fields"""
    profile_id: Optional[str] = None  # Nullable: sessions persist after profile deletion
    project_id: Optional[str] = None
    title: Optional[str] = None


class SessionCreate(SessionBase):
    """Session creation - typically automatic"""
    pass


class SessionTag(BaseModel):
    """Tag attached to a session (minimal tag info)"""
    id: str
    name: str
    color: str


class Session(SessionBase):
    """Full session response"""
    id: str
    sdk_session_id: Optional[str] = None
    status: str = "active"
    is_favorite: bool = False
    api_user_id: Optional[str] = None
    api_user_name: Optional[str] = None
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    cache_creation_tokens: int = 0  # Tokens used to create cache (counts toward context)
    cache_read_tokens: int = 0  # Tokens read from cache (doesn't count toward context)
    # Context window tokens = last_input + cache_creation + cache_read (current context size)
    context_tokens: int = 0
    turn_count: int = 0
    tags: List[SessionTag] = Field(default_factory=list)  # Tags attached to this session
    # Fork/branch fields
    parent_session_id: Optional[str] = None  # ID of parent session if this is a fork
    fork_point_message_index: Optional[int] = None  # Message index where fork occurred
    has_forks: bool = False  # True if this session has forked children
    # Worktree fields - for restoring worktree mode on session resume
    worktree_id: Optional[str] = None  # ID of linked worktree (if any)
    worktree_branch: Optional[str] = None  # Branch name of linked worktree
    worktree_path: Optional[str] = None  # Path to worktree directory
    worktree_deleted: bool = False  # True if worktree is not currently materialized; recreated on next send
    # V2 branch-intake (Phase 3): a NEW branch session records only its logical
    # branch here (no legacy worktree row); the deterministic per-runner checkout
    # is materialized lazily at spawn. Legacy worktree sessions leave this NULL and
    # keep using worktree_branch. The UI badge falls back to this when
    # worktree_branch is absent.
    branch: Optional[str] = None
    # Automation provenance — set when an automation run created this session.
    # Automation sessions run headless (no live watch surface), so the sessions
    # list badges them to differentiate from interactive chats.
    automation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # Cross-install (cluster) annotation — populated by the API enrichment layer
    # when sessions are shared across installs on a shared database.
    instance_id: Optional[str] = None  # Owning install (NULL = legacy/this install)
    device_name: Optional[str] = None  # Human-friendly owning-device name
    device_online: bool = True  # Owning install seen recently (heartbeat)
    is_remote: bool = False  # Owned by another install (not this one)
    controllable: bool = True  # Live send/interrupt reachable (owner online + has base_url)
    # V2 Q7: transient, derive-on-read (never persisted). True when a session has
    # a queued turn but NO capable runner is online to execute it — i.e. it is
    # waiting, read-only, until an owning device comes back. Derived by the API
    # enrichment layer via ``placement.resolve_placement`` (the single oracle) and
    # only when the ``v2_placement`` flag is ON; the model default is False.
    degraded: bool = False
    project_name: Optional[str] = None  # Resolved server-side (frontend $projects is instance-filtered)
    # Folds same-repo project clones across installs into ONE sessions-board card.
    # Derived from the git remote so every install agrees; without it on the
    # response model FastAPI strips it and the board falls back to project_id,
    # rendering the same repo as a separate card per install.
    link_group_id: Optional[str] = None
    # V2 projects (Phase 3): the origin-keyed project this session belongs to
    # (install-independent identity) and its origin token. Populated by the API
    # enrichment layer (unconditional since PR-B). ``origin`` is 1:1 with
    # ``link_group_id`` for grouped projects, so the board folds identically
    # whether it groups by ``origin`` or ``link_group_id`` (no regroup flash).
    project_v2_id: Optional[str] = None
    origin: Optional[str] = None
    # V2 streaming path marker. Forced TRUE on every session (V2 Phase 1
    # deletion); the model default stays False for unset/legacy construction.
    v2_stream: bool = False


class SessionWithMessages(Session):
    """Session with message history"""
    messages: List[SessionMessage] = []


class SessionSearchResult(Session):
    """Session search result with match information"""
    match_type: str  # 'title' or 'content'
    match_snippet: Optional[str] = None  # Snippet showing where the match was found
    match_time: Optional[datetime] = None  # When the matching content was created
    match_count: int = 1  # How many places matched (title/legacy/store); 1 for back-compat


class PaginatedSessions(BaseModel):
    """Paginated sessions response with total count for infinite scroll"""
    items: List[Session]
    total: int
    offset: int
    limit: int
    has_more: bool


class CursorPaginatedSessions(BaseModel):
    """Cursor-based paginated sessions response for efficient infinite scroll"""
    items: List[Session]
    next_cursor: Optional[str] = None  # updated_at of last item
    next_cursor_id: Optional[str] = None  # id of last item (tiebreaker)
    has_more: bool


class PaginatedSessionSearchResults(BaseModel):
    """Paginated session search results with total count"""
    items: List[SessionSearchResult]
    total: int
    offset: int
    limit: int
    has_more: bool


# ============================================================================
# Query Models
# ============================================================================

class QueryOverrides(BaseModel):
    """Optional overrides for a query"""
    model: Optional[str] = None
    permission_mode: Optional[str] = None  # default, auto, acceptEdits, plan, bypassPermissions
    system_prompt_append: Optional[str] = None
    max_turns: Optional[int] = None
    thinking_mode: Optional[str] = None  # default, disabled, adaptive, think, think_harder, ultra_think
    effort: Optional[str] = None  # low, medium, high, max, xhigh (xhigh only for Opus 4.7+)


class QueryRequest(BaseModel):
    """Query request"""
    prompt: str = Field(..., min_length=1)
    profile: str = "claude-code"
    project: Optional[str] = None
    overrides: Optional[QueryOverrides] = None


class ConversationRequest(BaseModel):
    """Continue or start a conversation"""
    prompt: str = Field(..., min_length=1)
    session_id: Optional[str] = None  # If provided, continues existing session
    profile: Optional[str] = "claude-code"  # Used only for new sessions
    project: Optional[str] = None  # Used only for new sessions
    overrides: Optional[QueryOverrides] = None
    device_id: Optional[str] = None  # Device identifier for cross-device sync


class QueryMetadata(BaseModel):
    """Query execution metadata"""
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    total_cost_usd: Optional[float] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    num_turns: Optional[int] = None


class QueryResponse(BaseModel):
    """Query response (non-streaming)"""
    response: str
    session_id: str
    metadata: QueryMetadata


# ============================================================================
# SSE Event Models
# ============================================================================

class SSETextEvent(BaseModel):
    """SSE text content event"""
    type: str = "text"
    content: str


class SSEToolUseEvent(BaseModel):
    """SSE tool use event"""
    type: str = "tool_use"
    name: str
    input: Dict[str, Any]


class SSEToolResultEvent(BaseModel):
    """SSE tool result event"""
    type: str = "tool_result"
    name: str
    output: str


class SSEDoneEvent(BaseModel):
    """SSE completion event"""
    type: str = "done"
    session_id: str
    metadata: QueryMetadata


class SSEErrorEvent(BaseModel):
    """SSE error event"""
    type: str = "error"
    message: str


# ============================================================================
# System Models
# ============================================================================

class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    service: str
    version: str
    authenticated: bool
    setup_required: bool
    claude_authenticated: bool


class VersionResponse(BaseModel):
    """Version information"""
    api_version: str
    claude_version: Optional[str] = None


class StatsResponse(BaseModel):
    """Usage statistics"""
    total_sessions: int
    total_queries: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int


# ============================================================================
# Rewind Models
# ============================================================================

class RewindCheckpoint(BaseModel):
    """A checkpoint that can be rewound to"""
    index: int
    message: str  # Truncated message for display
    full_message: Optional[str] = None
    timestamp: Optional[str] = None
    is_current: bool = False


class RewindRequest(BaseModel):
    """Request to execute a rewind operation"""
    checkpoint_index: int = Field(..., ge=0, description="Index of checkpoint to rewind to")
    restore_option: int = Field(..., ge=1, le=4, description="1=code+conversation, 2=conversation, 3=code, 4=cancel")
    checkpoint_message: Optional[str] = None


class RewindCheckpointsResponse(BaseModel):
    """Response containing available checkpoints"""
    success: bool
    session_id: str
    checkpoints: List[RewindCheckpoint] = []
    error: Optional[str] = None


class RewindExecuteResponse(BaseModel):
    """Response from executing a rewind"""
    success: bool
    message: str
    checkpoint_index: Optional[int] = None
    restore_option: Optional[int] = None
    error: Optional[str] = None


class RewindStatus(BaseModel):
    """Current rewind status"""
    has_pending: bool
    pending_rewind: Optional[Dict[str, Any]] = None


# ============================================================================
# Tag Models
# ============================================================================

class TagBase(BaseModel):
    """Base tag fields"""
    name: str = Field(..., min_length=1, max_length=50)
    color: str = Field(default="#6366f1", pattern=r'^#[0-9a-fA-F]{6}$')


class TagCreate(TagBase):
    """Tag creation request"""
    pass


class TagUpdate(BaseModel):
    """Tag update request"""
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    color: Optional[str] = Field(None, pattern=r'^#[0-9a-fA-F]{6}$')


class Tag(TagBase):
    """Full tag response"""
    id: str
    created_at: datetime
    updated_at: datetime


class SessionTagsUpdate(BaseModel):
    """Update tags for a session"""
    tag_ids: List[str] = Field(default_factory=list)


# ============================================================================
# Analytics Models
# ============================================================================

class UsageStats(BaseModel):
    """Aggregated usage statistics for a date range"""
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    session_count: int = 0
    query_count: int = 0


class CostBreakdownItem(BaseModel):
    """A single item in a cost breakdown"""
    key: str  # profile_id, api_user_id, or date string
    name: Optional[str] = None  # Display name (profile name, user name, etc.)
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    query_count: int = 0


class CostBreakdownResponse(BaseModel):
    """Cost breakdown response with grouping"""
    group_by: str  # 'profile', 'user', 'date'
    start_date: str
    end_date: str
    items: List[CostBreakdownItem] = []
    total_cost_usd: float = 0.0


class UsageTrend(BaseModel):
    """A single data point in a usage trend time series"""
    date: str  # ISO date string (YYYY-MM-DD or YYYY-WNN for weeks)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    query_count: int = 0


class UsageTrendsResponse(BaseModel):
    """Usage trends response with time series data"""
    interval: str  # 'day', 'week', 'month'
    start_date: str
    end_date: str
    data_points: List[UsageTrend] = []


class TopSession(BaseModel):
    """A session with high usage/cost"""
    session_id: str
    title: Optional[str] = None
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    created_at: Optional[str] = None


class AnalyticsSummaryResponse(BaseModel):
    """Combined analytics summary response"""
    start_date: str
    end_date: str
    usage_stats: UsageStats
    top_profiles: List[CostBreakdownItem] = []
    recent_trend: List[UsageTrend] = []


# ============================================================================
# Template Models
# ============================================================================

class TemplateBase(BaseModel):
    """Base template fields"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    prompt: str = Field(..., min_length=1)
    profile_id: Optional[str] = None  # Link to specific profile, null for all
    icon: Optional[str] = None  # Emoji or icon name
    category: Optional[str] = None  # For grouping (e.g., "coding", "writing")


class TemplateCreate(TemplateBase):
    """Template creation request"""
    pass


class TemplateUpdate(BaseModel):
    """Template update request - all fields optional"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    prompt: Optional[str] = Field(None, min_length=1)
    profile_id: Optional[str] = None
    icon: Optional[str] = None
    category: Optional[str] = None


class Template(TemplateBase):
    """Full template response"""
    id: str
    is_builtin: bool = False
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Webhook Models
# ============================================================================

class WebhookBase(BaseModel):
    """Base webhook fields"""
    url: str = Field(..., min_length=1)
    events: List[str] = Field(default_factory=list)
    secret: Optional[str] = None


class WebhookCreate(WebhookBase):
    """Webhook creation request"""
    pass


class WebhookUpdate(BaseModel):
    """Webhook update request - all fields optional"""
    url: Optional[str] = Field(None, min_length=1)
    events: Optional[List[str]] = None
    secret: Optional[str] = None
    is_active: Optional[bool] = None


class Webhook(WebhookBase):
    """Full webhook response"""
    id: str
    is_active: bool = True
    created_at: datetime
    last_triggered_at: Optional[datetime] = None
    failure_count: int = 0


class WebhookEventData(BaseModel):
    """Data payload within a webhook event"""
    session_id: Optional[str] = None
    title: Optional[str] = None
    profile_id: Optional[str] = None
    total_cost: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None


class WebhookEvent(BaseModel):
    """Webhook event payload structure"""
    event: str  # Event type: session.complete, session.error, etc.
    timestamp: str  # ISO format timestamp
    data: WebhookEventData


class WebhookTestResponse(BaseModel):
    """Response from testing a webhook"""
    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[int] = None
    error: Optional[str] = None


# ============================================================================
# Agent Export/Import Models
# ============================================================================

class AgentExportData(BaseModel):
    """Agent configuration data for export"""
    name: str
    description: Optional[str] = None
    model: Optional[str] = DEFAULT_MODEL
    permission_mode: Optional[str] = "default"  # default, auto, acceptEdits, plan, bypassPermissions
    thinking_mode: Optional[str] = "default"
    effort: Optional[str] = None  # low, medium, high, max, xhigh (xhigh only for Opus 4.7+)
    system_prompt_type: Optional[str] = "preset"
    system_prompt_preset: Optional[str] = "claude_code"
    system_prompt_content: Optional[str] = None
    system_prompt_append: Optional[str] = None
    system_prompt_inject_env: Optional[bool] = False
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    enabled_agents: Optional[List[str]] = None
    ai_tools: Optional[AIToolsConfig] = None
    max_turns: Optional[int] = None
    max_buffer_size: Optional[int] = None
    include_partial_messages: Optional[bool] = True
    continue_conversation: Optional[bool] = False
    fork_session: Optional[bool] = False
    setting_sources: Optional[List[str]] = None
    enable_file_checkpointing: Optional[bool] = False
    app_config_tools: Optional[bool] = False
    # Environment variables (exported with profile)
    env: Optional[Dict[str, str]] = None


class AgentExport(BaseModel):
    """Full agent export format"""
    version: str = "1.0"
    type: str = "ai-shuffle-agent"
    exported_at: str
    agent: AgentExportData


class AgentImportRequest(BaseModel):
    """Request to import an agent from JSON data"""
    version: str = Field(..., description="Export format version")
    type: str = Field(..., description="Must be 'ai-shuffle-agent'")
    exported_at: Optional[str] = None
    agent: AgentExportData

    # Optional override for profile ID (otherwise generated from name)
    new_id: Optional[str] = Field(None, pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)
    # Optional override for profile name
    new_name: Optional[str] = Field(None, min_length=1, max_length=100)


# ============================================================================
# AI Generation Models
# ============================================================================

class ClarificationQuestionModel(BaseModel):
    """A clarification question for the user"""
    id: str = Field(..., description="Question identifier")
    question: str = Field(..., description="The question to ask")
    options: Optional[List[str]] = Field(None, description="Optional predefined answer choices")
    context: Optional[str] = Field(None, description="Additional context about why this is asked")


class GenerationResponseModel(BaseModel):
    """Response from AI generation endpoints"""
    status: str = Field(..., description="'complete' or 'needs_clarification'")
    result: Optional[Dict[str, Any]] = Field(None, description="Generated config when complete")
    questions: Optional[List[ClarificationQuestionModel]] = Field(
        None, description="Questions to ask when clarification needed"
    )
    generation_type: Optional[str] = Field(None, description="'profile' or 'subagent'")


class ClarificationAnswersModel(BaseModel):
    """User's answers to clarification questions"""
    description: str = Field(..., min_length=10, max_length=2000, description="Original description")
    answers: Dict[str, str] = Field(..., description="Map of question_id -> user's answer")


class AuditLogEntry(BaseModel):
    """A single audit log entry"""
    id: str
    user_id: Optional[str] = None
    user_type: str = "admin"
    event_type: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: str


class AuditLogResponse(BaseModel):
    """Paginated audit log response"""
    entries: List[AuditLogEntry]
    total: int
    limit: int
    offset: int


# ============================================================================
# Rate Limit Models
# ============================================================================

class RateLimitConfigBase(BaseModel):
    """Base rate limit configuration fields"""
    requests_per_minute: int = 20
    requests_per_hour: int = 200
    requests_per_day: int = 1000
    concurrent_requests: int = 3
    priority: int = 0
    is_unlimited: bool = False


class RateLimitConfigCreate(RateLimitConfigBase):
    """Create a rate limit configuration"""
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None


class RateLimitConfigUpdate(BaseModel):
    """Update a rate limit configuration"""
    requests_per_minute: Optional[int] = None
    requests_per_hour: Optional[int] = None
    requests_per_day: Optional[int] = None
    concurrent_requests: Optional[int] = None
    priority: Optional[int] = None
    is_unlimited: Optional[bool] = None


class RateLimitConfig(RateLimitConfigBase):
    """Full rate limit configuration response"""
    id: str
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RateLimitStatus(BaseModel):
    """Current rate limit status for a user"""
    minute_count: int = 0
    minute_limit: int = 20
    minute_remaining: int = 20
    minute_reset: datetime
    hour_count: int = 0
    hour_limit: int = 200
    hour_remaining: int = 200
    hour_reset: datetime
    day_count: int = 0
    day_limit: int = 1000
    day_remaining: int = 1000
    day_reset: datetime
    concurrent_count: int = 0
    concurrent_limit: int = 3
    concurrent_remaining: int = 3
    is_limited: bool = False
    is_unlimited: bool = False
    priority: int = 0


class QueueStatus(BaseModel):
    """Queue position and wait time"""
    position: int = 0
    estimated_wait_seconds: int = 0
    total_queued: int = 0
    is_queued: bool = False


# ============================================================================
# Notes Models
# ============================================================================

class NoteBase(BaseModel):
    """Base note fields"""
    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(default="")
    tags: List[str] = Field(default_factory=list)  # User-defined tag names
    is_pinned: bool = False


class NoteCreate(NoteBase):
    """Note creation request"""
    pass


class NoteUpdate(BaseModel):
    """Note update request - all fields optional"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    is_pinned: Optional[bool] = None


class Note(NoteBase):
    """Full note response"""
    id: str
    file_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class NoteTag(BaseModel):
    """Aggregated tag information for notes"""
    name: str
    color: Optional[str] = None
    count: int


class NoteAutocomplete(BaseModel):
    """Simplified note for chat autocomplete"""
    id: str
    title: str
    preview: str  # First ~100 chars of content
    tags: List[str]
    file_path: Optional[str] = None
    absolute_path: Optional[str] = None


class NoteAttachment(BaseModel):
    """A file attached to a note, referenceable via @ in the note body"""
    id: str
    note_id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: int = 0
    created_at: datetime


# ============================================================================
# Hook Models
# ============================================================================

# Valid hook event types matching Claude Code events
VALID_HOOK_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreCompact",
    "SubagentStop",
    "Notification",
]


class HookBase(BaseModel):
    """Base hook fields"""
    name: str = Field(..., min_length=1, max_length=100)
    event: str = Field(..., description="Hook event type")
    hook_type: str = Field(..., pattern=r'^(bash|webhook|message)$', description="Type: 'bash', 'webhook', or 'message'")
    # For bash hooks
    command: Optional[str] = Field(None, description="Shell command to execute")
    timeout: int = Field(default=30000, ge=1000, le=300000, description="Timeout in ms (1-300 seconds)")
    # For webhook hooks
    url: Optional[str] = Field(None, description="Webhook URL to POST to")
    secret: Optional[str] = Field(None, description="HMAC secret for webhook signing")
    # For message hooks
    message_template: Optional[str] = Field(None, description="Prompt template sent as a message in the active chat")
    # Common
    enabled: bool = True
    tool_matcher: Optional[str] = Field(None, description="Tool name pattern for PreToolUse/PostToolUse (e.g., 'Bash|Edit')")


class HookCreate(HookBase):
    """Hook creation request"""
    pass


class HookUpdate(BaseModel):
    """Hook update request - all fields optional"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    event: Optional[str] = None
    hook_type: Optional[str] = Field(None, pattern=r'^(bash|webhook|message)$')
    command: Optional[str] = None
    timeout: Optional[int] = Field(None, ge=1000, le=300000)
    url: Optional[str] = None
    secret: Optional[str] = None
    message_template: Optional[str] = None
    enabled: Optional[bool] = None
    tool_matcher: Optional[str] = None


class Hook(HookBase):
    """Full hook response"""
    id: str
    project_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_triggered_at: Optional[datetime] = None
    failure_count: int = 0


class HookTestResult(BaseModel):
    """Result from testing a hook"""
    success: bool
    duration_ms: Optional[int] = None
    output: Optional[str] = None
    error: Optional[str] = None


class PluginHookInfo(BaseModel):
    """Read-only hook info from an installed plugin"""
    plugin_id: str
    plugin_name: str
    event: str
    hook_type: str  # 'prompt' or 'command'
    description: Optional[str] = None
    tool_matcher: Optional[str] = None


# ============================================================================
# Mobile Device Models
# ============================================================================

class DeviceCapabilities(BaseModel):
    """Mobile device capabilities"""
    supports_biometric: bool = False
    supports_push: bool = False
    biometric_types: List[str] = Field(default_factory=list)  # face_id, touch_id, fingerprint


class DeviceInfo(BaseModel):
    """Device registration info"""
    device_id: str = Field(..., min_length=1, max_length=255)
    device_name: str = Field(..., min_length=1, max_length=100)
    platform: str = Field(..., pattern=r'^(ios|android)$')
    capabilities: DeviceCapabilities


class MobileDeviceRegisterRequest(BaseModel):
    """Request to register a mobile device"""
    device_info: DeviceInfo


class MobileDeviceRegisterResponse(BaseModel):
    """Response after registering a mobile device"""
    api_key: str
    device_id: str
    expires_at: datetime


class MobileDeviceCapabilitiesRequest(BaseModel):
    """Request to update device capabilities"""
    device_id: str
    capabilities: DeviceCapabilities


class MobileDevice(BaseModel):
    """Full mobile device response"""
    id: str
    device_id: str
    device_name: str
    platform: str
    capabilities: DeviceCapabilities
    fcm_token: Optional[str] = None
    created_at: datetime
    last_seen: Optional[datetime] = None


class PendingActionStatus(BaseModel):
    """Status values for pending actions"""
    PENDING: str = "pending"
    APPROVED: str = "approved"
    REJECTED: str = "rejected"
    EXPIRED: str = "expired"


class PendingAction(BaseModel):
    """A pending action waiting for mobile confirmation"""
    id: str
    device_id: str
    action_type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    security_level: str = "normal"  # normal, high, critical
    created_at: datetime
    expires_at: datetime
    status: str = "pending"


class ActionConfirmRequest(BaseModel):
    """Request to confirm or reject an action"""
    approved: bool
    biometric_verified: bool = False


class PushTokenRequest(BaseModel):
    """Request to register FCM push token"""
    device_id: str
    fcm_token: str = Field(..., min_length=1)


class UnifiedPushEndpointRequest(BaseModel):
    """Request to register/update a device's UnifiedPush endpoint URL.

    The app gets this URL from its UnifiedPush distributor (e.g. ntfy) and
    registers it so a self-hosted server can deliver notifications without a
    shared Firebase project.
    """
    device_id: str
    endpoint: str = Field(..., min_length=1)


class TrustedAction(BaseModel):
    """A trusted action pattern for auto-approval"""
    id: str
    device_id: str
    action_pattern: str
    auto_approve: bool = True
    require_preview: bool = False


# ============================================================================
# API User Models
# ============================================================================

class ApiUserCreate(BaseModel):
    """API user creation request"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    project_id: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    web_login_allowed: bool = True


class ApiUserUpdate(BaseModel):
    """API user update request — all fields optional"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    project_id: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    is_active: Optional[bool] = None
    web_login_allowed: Optional[bool] = None


class ApiUserResponse(BaseModel):
    """Full API user response (never includes key or hash)"""
    id: str
    name: str
    username: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    is_active: bool = True
    web_login_allowed: bool = True
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None


class ApiUserCreateResponse(ApiUserResponse):
    """Response from create — includes plaintext API key shown ONCE"""
    api_key: str


class ApiUserRotateKeyResponse(BaseModel):
    """Response from key rotation — includes new plaintext API key shown ONCE"""
    id: str
    api_key: str


# ============================================================================
# Connector Models
# ============================================================================

class ConnectorType(str, Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    WEBHOOK = "webhook"
    SLACK = "slack"
    LINE = "line"
    SIGNAL = "signal"
    IMESSAGE = "imessage"
    MSTEAMS = "msteams"
    BLUEBUBBLES = "bluebubbles"
    SPOTIFY = "spotify"
    HUE = "hue"
    HOMEASSISTANT = "homeassistant"
    GMAIL = "gmail"


VALID_CONNECTOR_EVENTS = [
    "session.complete",
    "session.error",
    "session.started",
    "permission.requested",
    "input.needed",
]


class ConnectorConfigTelegram(BaseModel):
    """Telegram-specific connector config"""
    bot_token: str = Field(..., min_length=1)
    allowed_chat_ids: Optional[List[str]] = None
    welcome_message: Optional[str] = None


class ConnectorConfigDiscord(BaseModel):
    """Discord-specific connector config"""
    bot_token: str = Field(..., min_length=1)
    guild_ids: Optional[List[str]] = None
    allowed_channel_ids: Optional[List[str]] = None
    command_prefix: str = "!"


class ConnectorConfigWhatsApp(BaseModel):
    """WhatsApp Cloud API connector config"""
    phone_number_id: str = Field(..., min_length=1)
    access_token: str = Field(..., min_length=1)
    verify_token: str = Field(..., min_length=1)
    allowed_phone_numbers: Optional[List[str]] = None


class ConnectorConfigWebhook(BaseModel):
    """Generic webhook connector config (replaces old webhook system)"""
    url: str = Field(..., min_length=1)
    secret: Optional[str] = None
    headers: Optional[Dict[str, str]] = None


class ConnectorBase(BaseModel):
    """Base connector fields for creation/update"""
    name: str = Field(..., min_length=1, max_length=100)
    type: ConnectorType
    config: Dict[str, Any] = Field(default_factory=dict)
    events: List[str] = Field(default_factory=list)


class ConnectorCreate(ConnectorBase):
    """Connector creation request"""
    api_user_id: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    project_id: Optional[str] = None


class ConnectorUpdate(BaseModel):
    """Connector update request — all fields optional"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    config: Optional[Dict[str, Any]] = None
    events: Optional[List[str]] = None
    is_active: Optional[bool] = None


class Connector(ConnectorBase):
    """Full connector response"""
    id: str
    api_user_id: Optional[str] = None
    is_active: bool = False
    status: str = "disconnected"
    status_message: Optional[str] = None
    last_connected_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    failure_count: int = 0
    created_at: datetime
    updated_at: datetime


class ConnectorTestResponse(BaseModel):
    """Response from testing a connector"""
    success: bool
    message: str
    response_time_ms: Optional[int] = None
    error: Optional[str] = None


class ConnectorEventLog(BaseModel):
    """A single connector event log entry"""
    id: str
    connector_id: str
    event_type: str
    direction: str
    payload: Optional[Dict[str, Any]] = None
    status: str
    error: Optional[str] = None
    created_at: datetime


class ConnectorTriggerInfo(BaseModel):
    """Trigger definition from a connector type."""
    id: str
    name: str
    description: str
    payload_schema: Dict[str, Any] = Field(default_factory=dict)


class ConnectorActionInfo(BaseModel):
    """Action definition from a connector type."""
    id: str
    name: str
    description: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)


class ConnectorToolInfo(BaseModel):
    """AI tool definition from a connector type."""
    id: str
    name: str
    description: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Automation Models
# ============================================================================

class AutomationTriggerType(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    CONNECTOR_EVENT = "connector_event"
    WEBHOOK = "webhook"
    CONNECTOR_TRIGGER = "connector_trigger"


class AutomationOutputType(str, Enum):
    CONNECTOR = "connector"
    WEBHOOK = "webhook"
    SESSION = "session"
    NONE = "none"


VALID_AUTOMATION_TRIGGER_TYPES = [t.value for t in AutomationTriggerType]


class TriggerItem(BaseModel):
    """A single trigger within an automation's triggers array."""
    type: AutomationTriggerType
    config: Dict[str, Any] = Field(default_factory=dict)


class AutomationCreate(BaseModel):
    """Automation creation request"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    # Legacy single-trigger fields (still accepted for backward compat)
    trigger_type: Optional[AutomationTriggerType] = None
    trigger_config: Dict[str, Any] = Field(default_factory=dict)
    # Multi-trigger support
    triggers: Optional[List[TriggerItem]] = None
    prompt: Optional[str] = Field(default=None)
    action_steps: Optional[List[Dict[str, Any]]] = None
    profile_id: str
    project_id: Optional[str] = None
    output_config: Dict[str, Any] = Field(default_factory=lambda: {"type": "session"})
    max_turns: Optional[int] = Field(default=1, ge=1)  # None = unlimited
    timeout_seconds: Optional[int] = Field(default=300, ge=10)  # None = unlimited
    max_cost_per_run: Optional[float] = Field(default=None, ge=0)
    max_cost_per_day: Optional[float] = Field(default=None, ge=0)
    max_cost_total: Optional[float] = Field(default=None, ge=0)
    max_runs_per_day: Optional[int] = Field(default=None, ge=1)
    requires_approval: bool = False
    # Initial enabled state. Defaults to False to preserve the create-then-enable
    # UI flow; the create response always carries the real ``is_enabled`` so the
    # client can render truthfully.
    enabled: bool = False


class AutomationUpdate(BaseModel):
    """Automation update request — all fields optional"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    trigger_type: Optional[AutomationTriggerType] = None
    trigger_config: Optional[Dict[str, Any]] = None
    triggers: Optional[List[TriggerItem]] = None
    prompt: Optional[str] = None
    action_steps: Optional[List[Dict[str, Any]]] = None
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    output_config: Optional[Dict[str, Any]] = None
    max_turns: Optional[int] = Field(None, ge=1)  # explicit null = unlimited
    timeout_seconds: Optional[int] = Field(None, ge=10)  # explicit null = unlimited
    max_cost_per_run: Optional[float] = None
    max_cost_per_day: Optional[float] = None
    max_cost_total: Optional[float] = None
    max_runs_per_day: Optional[int] = None
    requires_approval: Optional[bool] = None
    is_enabled: Optional[bool] = None


class Automation(BaseModel):
    """Full automation response"""
    id: str
    name: str
    description: Optional[str] = None
    trigger_type: str
    trigger_config: Dict[str, Any] = Field(default_factory=dict)
    triggers: Optional[List[Dict[str, Any]]] = None
    prompt: Optional[str] = None
    action_steps: Optional[List[Dict[str, Any]]] = None
    profile_id: str
    project_id: Optional[str] = None
    output_config: Dict[str, Any] = Field(default_factory=lambda: {"type": "session"})
    max_turns: Optional[int] = 1  # None = unlimited
    timeout_seconds: Optional[int] = 300  # None = unlimited
    max_cost_per_run: Optional[float] = None
    max_cost_per_day: Optional[float] = None
    max_cost_total: Optional[float] = None
    max_runs_per_day: Optional[int] = None
    total_cost_used: float = 0.0
    is_enabled: bool = False
    status: str = "idle"
    last_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
    run_count: int = 0
    failure_count: int = 0
    requires_approval: bool = False
    created_at: datetime
    updated_at: datetime


class AutomationRun(BaseModel):
    """Automation run response"""
    id: str
    automation_id: str
    status: str
    trigger_data: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    prompt_rendered: Optional[str] = None
    response: Optional[str] = None
    error: Optional[str] = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    output_status: str = "pending"
    output_error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime


class AutomationApproval(BaseModel):
    """Automation approval response"""
    id: str
    run_id: str
    automation_id: str
    automation_name: Optional[str] = None
    status: str
    prompt_preview: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


class AutomationStats(BaseModel):
    """Automation statistics"""
    automation_id: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    total_cost_usd: float = 0.0
    avg_duration_ms: float = 0.0
    today_runs: int = 0
    today_cost_usd: float = 0.0


class AutomationTestResult(BaseModel):
    """Result of a dry-run test"""
    prompt_rendered: str
    budget_ok: bool
    message: str
