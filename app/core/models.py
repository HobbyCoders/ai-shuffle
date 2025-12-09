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

class AIToolsConfig(BaseModel):
    """Configuration for individual AI tools - each tool can be enabled/disabled"""
    # Image tools (Nano Banana)
    image_generation: bool = False  # Generate AI images from text prompts
    image_editing: bool = False  # Edit existing images
    image_reference: bool = False  # Reference-based generation (character/style consistency)
    # Video tools (Veo)
    video_generation: bool = False  # Generate AI videos from text prompts
    image_to_video: bool = False  # Animate a still image into a video
    video_extend: bool = False  # Extend existing Veo videos
    video_bridge: bool = False  # Generate smooth transitions between two images


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


class Session(SessionBase):
    """Full session response"""
    id: str
    sdk_session_id: Optional[str] = None
    status: str = "active"
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    cache_creation_tokens: int = 0  # Tokens used to create cache (counts toward context)
    cache_read_tokens: int = 0  # Tokens read from cache (doesn't count toward context)
    # Context window tokens = last_input + cache_creation + cache_read (current context size)
    context_tokens: int = 0
    turn_count: int = 0
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
