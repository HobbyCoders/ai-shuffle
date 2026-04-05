/**
 * Standalone types that have no associated API functions in client.ts.
 * These are used as type imports by consumer files throughout the app.
 */

// Auth types
// NOTE: AuthStatus is defined here because auth.ts imports it from the barrel.
// auth.ts has its own function-specific types but uses this one from client.ts.
export interface AuthStatus {
	authenticated: boolean;
	is_admin: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
	github_authenticated: boolean;
	onboarding_complete: boolean;
	username: string | null;
}

// Profile types
export interface ProfileConfig {
	model?: string;
	allowed_tools?: string[];
	disallowed_tools?: string[];
	tools?: string[];  // SDK 0.1.12+ - explicit tool list
	tools_preset?: string;  // SDK 0.1.12+ - "claude_code" preset
	permission_mode?: string;
	max_turns?: number;
	include_partial_messages?: boolean;
	enable_file_checkpointing?: boolean;  // SDK 0.1.15+ - enable file snapshots for rewind
	thinking_mode?: 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think';  // Thinking config: default=SDK decides, disabled=off, adaptive=auto, think/think_harder/ultra_think=token budgets
	effort?: 'low' | 'medium' | 'high' | 'max';  // Effort level for reasoning depth (independent of thinking config)
	ai_tools?: Record<string, boolean>;  // AI tool toggles (image_generation, video_generation, etc.)
	enabled_agents?: string[];  // Subagents enabled for this profile
	setting_sources?: string[];  // Where to load settings from (e.g., 'claude_code')
	cwd?: string;  // Working directory override
	add_dirs?: string[];  // Additional directories to include
	user?: string;  // User identifier override
	max_buffer_size?: number;  // Maximum buffer size for output
	env?: Record<string, string>;  // Environment variables to set
	app_config_tools?: boolean;  // Allow AI to configure hooks, automations, MCP, profiles via chat
	system_prompt?: {
		type: string;
		preset?: string;
		content?: string;
		append?: string;
		inject_env_details?: boolean;
	} | null;
}

export interface Profile {
	id: string;
	name: string;
	description: string | null;
	is_builtin: boolean;
	session_count?: number;  // Number of conversations using this profile
	config: ProfileConfig;
	created_at: string;
	updated_at: string;
}

// Query types
export interface QueryRequest {
	prompt: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface ConversationRequest {
	prompt: string;
	session_id?: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface QueryResponse {
	response: string;
	session_id: string;
	metadata: {
		model?: string;
		duration_ms?: number;
		total_cost_usd?: number;
		num_turns?: number;
	};
}

// Health types
export interface HealthResponse {
	status: string;
	service: string;
	version: string;
	authenticated: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
}

// Permission types
export interface PermissionRequest {
	request_id: string;
	tool_name: string;
	tool_input: Record<string, unknown>;
	queue_position?: number;
	queue_total?: number;
	created_at?: string;
}

export interface PermissionResponse {
	request_id: string;
	decision: 'allow' | 'deny';
	remember?: 'none' | 'session' | 'profile';
	pattern?: string;
}

export interface PermissionRule {
	id: string;
	profile_id: string | null;
	tool_name: string;
	tool_pattern: string | null;
	decision: 'allow' | 'deny';
	created_at: string;
}

export interface PermissionQueueUpdate {
	resolved_ids: string[];
	remaining_count: number;
}

// User Question types (for AskUserQuestion tool)
export interface UserQuestionOption {
	label: string;
	description: string;
}

export interface UserQuestion {
	question: string;
	header: string;
	options: UserQuestionOption[];
	multiSelect: boolean;
}

export interface UserQuestionRequest {
	request_id: string;
	tool_use_id: string;
	questions: UserQuestion[];
	created_at?: string;
}

export interface UserQuestionResponse {
	request_id: string;
	tool_use_id: string;
	answers: Record<string, string | string[]>;
	remaining_count: number;
}

// Workspace configuration types
export interface WorkspaceConfig {
	workspace_path: string;
	is_configured: boolean;
	is_local_mode: boolean;
	default_path: string;
	exists: boolean;
}

export interface WorkspaceValidation {
	valid: boolean;
	error: string | null;
	path: string;
	exists: boolean;
	writable: boolean;
}
