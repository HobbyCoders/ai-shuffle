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

// Profile types — fluid harness options only. SDK-era keys (thinking_mode,
// setting_sources, max_buffer_size, user, strict_mcp_config, …) were dropped
// from the editor; the backend tolerates them on old rows but the fluid
// harness ignores them. effort and max_turns ARE honored (adapter maps them
// onto --effort / provider effort params and the per-turn model-call cap).
export interface ProfileConfig {
	model?: string;
	provider?: string | null;
	provider_models?: Record<string, string>;  // subagent model by provider; __native__ = native Claude, missing key = unavailable
	allowed_tools?: string[];
	disallowed_tools?: string[];
	permission_mode?: string;
	effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max';  // Reasoning depth — delegated CLI --effort / native provider effort param; chat can override per-turn
	max_turns?: number;  // Per-turn model-call cap (native legs → fluid max_model_calls_per_turn)
	include_partial_messages?: boolean;
	enable_file_checkpointing?: boolean;  // LEGACY (SDK-era): still read by chat rewind gating for old rows; no longer written
	ai_tools?: Record<string, boolean>;  // AI tool toggles (image_generation, video_generation, etc.)
	enabled_agents?: string[];  // Agents declared into this agent's context (name+description); empty/unset = all. NEVER a dispatch restriction
	cwd?: string;  // Working directory override
	add_dirs?: string[];  // Additional directories the agent may access
	env?: Record<string, string>;  // Environment variables to set
	app_config_tools?: boolean;  // Allow AI to configure hooks, automations, MCP, profiles via chat
	system_prompt?: {
		type: string;  // always 'custom' when written by the editor; 'preset' only on legacy rows
		preset?: string;  // LEGACY
		content?: string;
		append?: string;  // LEGACY: preset-shape append text
		inject_env_details?: boolean;  // LEGACY: env is fluid-compiled now
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
	session_id?: string;
	tool_name: string;
	tool_input: Record<string, unknown>;
	queue_position?: number;
	queue_total?: number;
	created_at?: string;
	// SDK 0.1.81 enrichment fields (Slice D) — optional; backend only forwards
	// when the SDK populates them on PermissionRequest payloads.
	title?: string;
	display_name?: string;
	description?: string;
	decision_reason?: string;
	blocked_path?: string;
}

export interface PermissionResponse {
	request_id: string;
	decision: 'allow' | 'deny' | 'defer';
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
	session_id?: string;
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
