/** The editable shape of one agent on the Agents page (ProfileCard owns it). */
export interface AgentForm {
	id: string;
	name: string;
	description: string;
	/** Per-provider model used when this agent is dispatched as a subagent. */
	provider_models: Record<string, string>;
	max_turns: number | null;
	allowed_tools: string[];
	disallowed_tools: string[];
	enabled_agents: string[];
	/** 'all' = may invoke any agent; 'custom' = enabled_agents is the exact allowlist (empty = none). */
	agent_invoke_mode: 'all' | 'custom';
	include_partial_messages: boolean;
	system_prompt_content: string;
	cwd: string;
	add_dirs: string;
	env_vars: { key: string; value: string }[];
	/** Setting sources (fluid scope filter). null = all scopes (default). */
	setting_sources: string[] | null;
	/** Avatar colour name; null = default. Saved only when the API has the field. */
	avatar_color: string | null;
}

/** A DB agent as GET /agents lists it (the roster of subagents). */
export interface RosterAgent {
	id: string;
	name: string;
	description: string;
	config?: {
		model?: string;
		provider?: string | null;
		provider_models?: Record<string, string>;
	};
	is_builtin: boolean;
}

export interface AgentTemplate {
	id: string;
	name: string;
	description: string;
	prompt: string;
	tools?: string[];
}

export interface GroupState {
	profiles?: {
		groups?: { name: string }[];
		assignments?: Record<string, string>;
	};
}

/** The detail tabs, in order. */
export const AGENT_TABS = [
	{ id: 'profile', label: 'Profile' },
	{ id: 'prompt', label: 'Instructions' },
	{ id: 'tools', label: 'Tools' },
	{ id: 'subagents', label: 'Subagents' },
	{ id: 'models', label: 'Models' },
	{ id: 'advanced', label: 'Advanced' }
] as const;
export type AgentTab = (typeof AGENT_TABS)[number]['id'];

export function isAgentTab(value: unknown): value is AgentTab {
	return AGENT_TABS.some((t) => t.id === value);
}
