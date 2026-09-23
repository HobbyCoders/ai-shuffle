/**
 * Which credential Claude Code runs on: the user's Claude subscription (the
 * stock CLI's own sign-in) or the Anthropic API key stored on this worker.
 *
 * The mode is an explicit choice. AI Shuffle never switches it on its own,
 * whichever credentials exist.
 */

import { api, type AgentGateStatus } from './client';

export type ClaudeAuthMode = 'subscription' | 'api_key';

export interface ClaudeAuthSummary {
	mode: ClaudeAuthMode;
	mode_label: string;
	/** False only before the server has recorded the default mode. */
	explicit: boolean;
	modes: { value: ClaudeAuthMode; label: string }[];
	/** What `claude auth status` reports; the account only when signed in. */
	cli: {
		installed: boolean;
		signed_in: boolean;
		email: string | null;
		org_name: string | null;
		plan: string | null;
		auth_method_label: string | null;
		error: string | null;
	};
	/** Whether a key is stored, and its last four characters. Never the key. */
	api_key: { set: boolean; last4: string | null };
	/** Whether new Claude agent runs can start on this worker, and why not. */
	gate?: AgentGateStatus;
}

export function getClaudeAuth(refresh = false): Promise<ClaudeAuthSummary> {
	return api.get(`/settings/claude-auth${refresh ? '?refresh=true' : ''}`, { cache: 'no-store' });
}

export function setClaudeAuthMode(mode: ClaudeAuthMode): Promise<ClaudeAuthSummary> {
	return api.put('/settings/claude-auth', { mode });
}
