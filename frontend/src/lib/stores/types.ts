/**
 * Shared Store Types
 *
 * Type definitions shared across all stores and components.
 * Extracted from tabs/types.ts so consumers don't need to pull in the tabs store.
 */

import type { Session, Profile, PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';

// Re-export API types that are used throughout components
export type { Session, Profile, PermissionRequestType, UserQuestionRequest };

// ─── API & Message Types ─────────────────────────────────────────────────────

export type MessageType = 'text' | 'thinking' | 'tool_use' | 'tool_result' | 'system' | 'subagent' | 'team_member' | 'command' | 'plugin_warning';

export type TeammateStatus = 'running' | 'completed' | 'error' | 'killed';

// Subagent child message - represents a single tool call or text from a subagent
export interface SubagentChildMessage {
	id: string;
	type: 'text' | 'tool_use' | 'tool_result';
	content: string;
	streaming?: boolean; // Whether this child is still streaming
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	partialToolInput?: string; // Streaming JSON accumulator for incomplete tool_input
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

// Queued message for "Send Later" feature
export interface QueuedMessage {
	id: string;
	content: string;
	queuedAt: number;
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
	agentUsage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number; input_tokens?: number; output_tokens?: number }; // Task progress stats
	// Team member fields (Claude Code agent-teams feature)
	teamName?: string;
	teammateName?: string;
	teammateSessionId?: string;
	teammatePid?: number;
	teammateStatus?: TeammateStatus;
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
