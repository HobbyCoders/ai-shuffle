/**
 * Tab Store Types
 *
 * Shared type definitions for the tabs store and its sub-modules.
 * Extracted from tabs.ts to reduce file size and improve maintainability.
 */

import type { Session, Profile, PermissionRequest as PermissionRequestType, UserQuestionRequest, SessionSearchResult } from '$lib/api/client';

// ─── API & Message Types ─────────────────────────────────────────────────────

export interface ApiUser {
	id: string;
	name: string;
	description?: string;
	project_id?: string;
	profile_ids?: string[];
	is_active: boolean;
	web_login_allowed?: boolean;
	username?: string;
	created_at: string;
	updated_at: string;
	last_used_at?: string;
}

export type MessageType = 'text' | 'thinking' | 'tool_use' | 'tool_result' | 'system' | 'subagent' | 'command' | 'plugin_warning';

// Subagent child message - represents a single tool call or text from a subagent
export interface SubagentChildMessage {
	id: string;
	type: 'text' | 'tool_use' | 'tool_result';
	content: string;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	timestamp?: string;
}

// Todo item for task tracking
export interface TodoItem {
	content: string;
	status: 'pending' | 'in_progress' | 'completed';
	activeForm: string; // Present continuous form shown when in_progress
}

export interface ChatMessage {
	id: string;
	jsonlUuid?: string; // Raw UUID from JSONL file - used for rewind (id may be preserved for Svelte keying)
	role: 'user' | 'assistant' | 'system';
	content: string;
	type?: MessageType;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use messages (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	metadata?: Record<string, unknown>;
	streaming?: boolean;
	partialToolInput?: string; // Accumulator for streaming tool input JSON
	systemSubtype?: string; // For system messages (e.g., 'context' for /context command)
	systemData?: Record<string, unknown>; // Raw data from system message
	profileId?: string; // Profile ID that was active when this message was created (for per-query tracking)
	// Subagent-specific fields
	agentId?: string; // The subagent's unique ID (e.g., '00ed8f4d')
	agentType?: string; // The subagent type (e.g., 'Explore', 'Plan')
	agentDescription?: string; // Task description from the Task tool
	agentPrompt?: string; // The initial prompt sent to the subagent
	agentStatus?: 'pending' | 'running' | 'completed' | 'error'; // Current status
	agentChildren?: SubagentChildMessage[]; // Nested messages from subagent execution
	// Command-specific fields (for slash commands like /commit, /ralph-loop)
	commandId?: string; // Internal command identifier (e.g., 'commit-commands:commit')
	commandName?: string; // Display name with slash (e.g., '/commit')
	commandArgs?: string; // Arguments passed to the command
}

// ─── Project & Execution Types ───────────────────────────────────────────────

export interface Project {
	id: string;
	name: string;
	description?: string;
	path: string;
	settings: Record<string, unknown>;
	session_count?: number;  // Number of sessions in this project
	created_at: string;
	updated_at: string;
}

// Execution mode for chat sessions
export type ExecutionMode = 'local' | 'worktree';

// ─── Chat Tab ────────────────────────────────────────────────────────────────

export interface ChatTab {
	id: string;
	title: string;
	sessionId: string | null;
	messages: ChatMessage[];
	isStreaming: boolean;
	wsConnected: boolean;
	error: string | null;
	profile: string;  // Current UI selection for next query
	sessionProfileId: string | null;  // Profile that created the session (for fallback display)
	currentQueryProfileId: string | null;  // Profile for the current streaming query
	project: string;
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	// Actual context window usage from /context command (source of truth)
	contextUsed: number | null;
	contextMax: number;
	// Session overrides (override profile settings for this session)
	modelOverride: string | null;  // null = use profile default
	permissionModeOverride: string | null;  // null = use profile default
	thinkingModeOverride: string | null;  // null = use profile default
	// Permission requests queue
	pendingPermissions: PermissionRequestType[];
	// User questions queue (for AskUserQuestion tool)
	pendingQuestions: UserQuestionRequest[];
	// Todo list for task tracking
	todos: TodoItem[];
	// Draft input text (preserved when switching tabs)
	draft: string;
	// Scroll position (preserved when switching tabs)
	scrollTop: number;
	// Stream start time for duration tracking (notifications)
	streamStartTime: number | null;
	// Execution mode settings (per-tab)
	executionMode: ExecutionMode;
	worktreeBranch: string;      // The branch name for worktree mode
	worktreeBaseBranch: string;  // Branch to base from (and PR target)
	worktreeCreateNewBranch: boolean;  // True if creating a new branch, false if using existing
	// New worktree model: existing worktree selection
	worktreeId: string | null;   // Selected existing worktree ID (null = create new)
	worktreeMode: 'new' | 'existing';  // Whether creating new worktree or using existing worktree
	selectedWorktreeBranch: string | null;  // Branch name of selected existing worktree (for display)
	// Worktree creation state (for deferred creation on message send)
	isCreatingWorktree: boolean;  // True while creating worktree before first message
	worktreeError: string | null; // Specific error for worktree creation
	worktreeDeleted: boolean;     // True if worktree was cleaned up after Finish (chat is read-only)
	// Session lock state (multi-device conflict)
	sessionLocked: boolean;      // True if session is locked by another device
	sessionLockedBy: string | undefined;  // Device ID that has the lock
	// Internal: tracks the last session update ID merged into this tab object.
	// Used by the _merged derived store to skip re-creating tab objects when
	// their session data hasn't changed (prevents cross-tab reactive cascade).
	_lastMergedUpdateId?: number;
}

// ─── Tabs State ──────────────────────────────────────────────────────────────

export interface TabsState {
	tabs: ChatTab[];
	activeTabId: string | null;
	profiles: Profile[];
	projects: Project[];
	sessions: Session[];
	adminSessions: Session[];
	apiUsers: ApiUser[];
	adminSessionsFilter: string | null; // null = all, '' = admin only, 'user_id' = specific user
	// Selection state for batch operations
	selectedSessionIds: Set<string>;
	selectedAdminSessionIds: Set<string>;
	selectionMode: boolean;
	adminSelectionMode: boolean;
	// Loading state to prevent race conditions
	sessionsLoading: boolean;
	// Tag filter state
	sessionsTagFilter: string | null; // null = all, 'tag_id' = filter by tag
	adminSessionsTagFilter: string | null;
	// Pagination state for sessions
	sessionsTotal: number;
	sessionsOffset: number;
	sessionsHasMore: boolean;
	sessionsLoadingMore: boolean;
	// Search state
	searchQuery: string;
	searchResults: SessionSearchResult[];
	searchTotal: number;
	searchOffset: number;
	searchHasMore: boolean;
	searchLoading: boolean;
	searchLoadingMore: boolean;
}

// ─── Persistence Types ───────────────────────────────────────────────────────

// Interface for persisted tab state (only what we need to restore tabs)
export interface PersistedTab {
	id: string;
	title: string;
	sessionId: string | null;
	profile: string;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
	// Worktree fields for restoring worktree mode on app resume
	executionMode?: ExecutionMode;
	worktreeId?: string | null;
	worktreeBranch?: string;
	selectedWorktreeBranch?: string | null;
	worktreeMode?: 'new' | 'existing';
	worktreeDeleted?: boolean;
}

export interface PersistedTabsState {
	tabs: PersistedTab[];
	activeTabId: string | null;
}
