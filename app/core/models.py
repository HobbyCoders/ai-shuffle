"""
Pydantic models for API requests and responses
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
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


class ApiKeyLoginRequest(BaseModel):
    """API key login request for web UI"""
    api_key: str


class ApiUserRegisterRequest(BaseModel):
    """API user registration request - claim an API key with username/password"""
    api_key: str
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class ApiUserLoginRequest(BaseModel):
    """API user login request with username/password"""
    username: str
    password: str


class ApiUserInfo(BaseModel):
    """API user info in auth responses"""
    id: str
    name: str
    project_id: Optional[str] = None
    profile_id: Optional[str] = None


class AuthStatus(BaseModel):
    """Authentication status response"""
    authenticated: bool
    is_admin: bool = True
    setup_required: bool
    claude_authenticated: bool
    username: Optional[str] = None
    api_user: Optional[ApiUserInfo] = None


# ============================================================================
# Subagent Models
# ============================================================================

class SubagentDefinition(BaseModel):
    """Definition for a subagent that can be invoked by the main agent"""
    description: str = Field(..., min_length=1, description="When to use this agent")
    prompt: str = Field(..., min_length=1, description="System prompt for the agent")
    tools: Optional[List[str]] = Field(None, description="Allowed tools (inherits all if not specified)")
    model: Optional[str] = Field(None, description="Model override: sonnet, opus, haiku, or inherit")


# ============================================================================
# Profile Models
# ============================================================================

# AIToolsConfig is dynamically generated from the AI_TOOLS registry
# This ensures the model always matches available tools - no manual sync needed
from app.core.ai_tools import AIToolsConfig  # noqa: E402


class SystemPromptConfig(BaseModel):
    """System prompt configuration"""
    type: str = "preset"  # "preset" or "custom"
    preset: Optional[str] = "claude_code"
    content: Optional[str] = None
    append: Optional[str] = None
    inject_env_details: bool = False  # Inject environment info (working dir, platform, date, etc.)


class ProfileConfig(BaseModel):
    """Claude Agent configuration stored in profile - maps to ClaudeAgentOptions"""
    # Core settings
    model: Optional[str] = "sonnet"
    permission_mode: Optional[str] = "default"  # default, acceptEdits, plan, bypassPermissions
    max_turns: Optional[int] = None

    # Tool configuration
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None

    # AI tools configuration - individual toggles for each AI tool
    ai_tools: Optional[AIToolsConfig] = None

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


class ProfileBase(BaseModel):
    """Base profile fields"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    config: ProfileConfig


class ProfileCreate(ProfileBase):
    """Profile creation request"""
    id: str = Field(..., pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)


class ProfileUpdate(BaseModel):
    """Profile update request"""
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[ProfileConfig] = None


class Profile(ProfileBase):
    """Full profile response"""
    id: str
    is_builtin: bool = False
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
    settings: Optional[ProjectSettings] = None


class ProjectUpdate(BaseModel):
    """Project update request"""
    name: Optional[str] = None
    description: Optional[str] = None
    settings: Optional[ProjectSettings] = None


class Project(ProjectBase):
    """Full project response"""
    id: str
    path: str
    settings: ProjectSettings
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
    timestamp: Optional[str] = None


class SessionMessage(BaseModel):
    """A message in a session"""
    id: Any  # Can be int from DB or string from JSONL
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
    metadata: Optional[Dict[str, Any]] = None
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
    profile_id: str
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
    created_at: datetime
    updated_at: datetime


class SessionWithMessages(Session):
    """Session with message history"""
    messages: List[SessionMessage] = []


class SessionSearchResult(Session):
    """Session search result with match information"""
    match_type: str  # 'title' or 'content'
    match_snippet: Optional[str] = None  # Snippet showing where the match was found
    match_time: Optional[datetime] = None  # When the matching content was created


# ============================================================================
# Query Models
# ============================================================================

class QueryOverrides(BaseModel):
    """Optional overrides for a query"""
    model: Optional[str] = None
    permission_mode: Optional[str] = None  # default, acceptEdits, plan, bypassPermissions
    system_prompt_append: Optional[str] = None
    max_turns: Optional[int] = None


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
# API User Models
# ============================================================================

class ApiUserBase(BaseModel):
    """Base API user fields"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    project_id: Optional[str] = None
    profile_id: Optional[str] = None
    web_login_allowed: bool = True  # Whether user can login to web UI


class ApiUserCreate(ApiUserBase):
    """API user creation request"""
    pass


class ApiUserUpdate(BaseModel):
    """API user update request"""
    name: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[str] = None
    profile_id: Optional[str] = None
    is_active: Optional[bool] = None
    web_login_allowed: Optional[bool] = None


class ApiUser(ApiUserBase):
    """Full API user response (without sensitive data)"""
    id: str
    is_active: bool = True
    username: Optional[str] = None  # Set when user registers
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None


class ApiUserWithKey(ApiUser):
    """API user response with newly generated key (only on create)"""
    api_key: str  # Only returned once on creation


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
    model: Optional[str] = "sonnet"
    permission_mode: Optional[str] = "default"
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
# Security / 2FA Models
# ============================================================================

class TwoFactorSetupResponse(BaseModel):
    """Response when starting 2FA setup"""
    secret: str  # Base32 secret for manual entry
    qr_code: str  # Base64-encoded QR code image
    uri: str  # otpauth:// URI


class TwoFactorVerifyRequest(BaseModel):
    """Request to verify and enable 2FA"""
    code: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')


class TwoFactorDisableRequest(BaseModel):
    """Request to disable 2FA"""
    code: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')
    password: str  # Require password confirmation


class TwoFactorStatusResponse(BaseModel):
    """Current 2FA status"""
    enabled: bool
    has_recovery_codes: bool
    recovery_codes_count: int = 0
    verified_at: Optional[str] = None


class RecoveryCodesResponse(BaseModel):
    """Response containing new recovery codes"""
    codes: List[str]
    count: int


class TwoFactorLoginRequest(BaseModel):
    """Request to verify 2FA during login"""
    code: str  # 6-digit TOTP code or recovery code
    use_recovery_code: bool = False


class LoginResponseWith2FA(BaseModel):
    """Login response that may require 2FA"""
    status: str  # "ok" or "2fa_required"
    message: str
    requires_2fa: bool = False
    is_admin: bool = False
    pending_2fa_token: Optional[str] = None  # Temporary token for 2FA verification


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
# Knowledge Base Models
# ============================================================================

class KnowledgeDocumentBase(BaseModel):
    """Base knowledge document fields"""
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default="text/plain")


class KnowledgeDocumentCreate(KnowledgeDocumentBase):
    """Knowledge document creation - content provided separately via file upload"""
    pass


class KnowledgeDocument(KnowledgeDocumentBase):
    """Full knowledge document response"""
    id: str
    project_id: str
    content: str
    file_size: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentSummary(BaseModel):
    """Knowledge document summary (without full content)"""
    id: str
    project_id: str
    filename: str
    content_type: str
    file_size: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeChunk(BaseModel):
    """A chunk of a knowledge document"""
    id: str
    document_id: str
    chunk_index: int
    content: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime


class KnowledgeSearchResult(BaseModel):
    """Search result from knowledge base"""
    id: str
    document_id: str
    filename: str
    chunk_index: int
    content: str
    relevance_score: float = 0.0
    metadata: Optional[Dict[str, Any]] = None


class KnowledgeStats(BaseModel):
    """Statistics for a project's knowledge base"""
    document_count: int = 0
    total_size: int = 0
    total_chunks: int = 0


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
