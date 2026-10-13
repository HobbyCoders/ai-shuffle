/**
 * Who an agent can call as a subagent ("Who it can call" on the Subagents tab).
 * 'all' = every other agent; 'custom' = exactly `enabled_agents` ("Selected
 * only"); 'none' = no subagents at all (the server takes Task and Swarm off the
 * agent's surface).
 */
export type AgentInvokeMode = 'all' | 'custom' | 'none';

/**
 * The mode a stored agent config (or an export's flat fields) is in. A stored
 * mode wins; legacy rows with no mode infer it from the allowlist: a non-empty
 * list meant "restrict to these", an empty one meant "any agent". A stored
 * "Selected only" with nothing selected stays "Selected only" (0 of N).
 */
export function invokeModeOf(mode: unknown, enabledIds: unknown): AgentInvokeMode {
	if (mode === 'all' || mode === 'custom' || mode === 'none') return mode;
	return Array.isArray(enabledIds) && enabledIds.length > 0 ? 'custom' : 'all';
}

/**
 * The form fields a mode pick changes. Picking None clears the selection (the
 * owner's call: an explicit None with no leftover picks), so switching back to
 * "Selected only" starts from nothing selected.
 */
export function invokeModePatch(mode: AgentInvokeMode): Partial<AgentForm> {
	return mode === 'none' ? { agent_invoke_mode: 'none', enabled_agents: [] } : { agent_invoke_mode: mode };
}

/** The header's "Subagents" fact: None, All N, or k of N (N = other agents). */
export function subagentsFact(mode: AgentInvokeMode, others: number, reach: number): string {
	if (mode === 'none' || others === 0) return 'None';
	return mode === 'custom' ? `${reach} of ${others}` : `All ${others}`;
}

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
	/** See AgentInvokeMode. Saved explicitly so "Selected only" with nothing selected and None both round-trip. */
	agent_invoke_mode: AgentInvokeMode;
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
