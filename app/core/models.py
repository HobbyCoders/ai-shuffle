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


class AuthStatus(BaseModel):
    """Authentication status response"""
    authenticated: bool
    setup_required: bool
    claude_authenticated: bool
    username: Optional[str] = None


# ============================================================================
# Profile Models
# ============================================================================

class SystemPromptConfig(BaseModel):
    """System prompt configuration"""
    type: str = "preset"  # "preset" or "custom"
    preset: Optional[str] = "claude_code"
    content: Optional[str] = None
    append: Optional[str] = None


class ProfileConfig(BaseModel):
    """Claude Agent configuration stored in profile - maps to ClaudeAgentOptions"""
    # Core settings
    model: Optional[str] = "sonnet"
    permission_mode: Optional[str] = "default"  # default, acceptEdits, plan, bypassPermissions
    max_turns: Optional[int] = None

    # Tool configuration
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None

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

class SessionMessage(BaseModel):
    """A message in a session"""
    id: int
    role: str  # user, assistant, system, tool
    content: str
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime


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
    turn_count: int = 0
    created_at: datetime
    updated_at: datetime


class SessionWithMessages(Session):
    """Session with message history"""
    messages: List[SessionMessage] = []


# ============================================================================
# Query Models
# ============================================================================

class QueryOverrides(BaseModel):
    """Optional overrides for a query"""
    model: Optional[str] = None
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
