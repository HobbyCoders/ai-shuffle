/**
 * Which credential Claude Code runs on: the user's Claude subscription (the
 * stock CLI's own sign-in), the Anthropic API key stored on this worker, or
 * off (Claude models leave the picker and Claude turns are refused).
 *
 * The mode is an explicit choice. AI Shuffle never switches it on its own,
 * whichever credentials exist.
 */

import { api, type AgentGateStatus } from './client';

export type ClaudeAuthMode = 'subscription' | 'api_key' | 'off';

export const CLAUDE_AUTH_MODES: readonly ClaudeAuthMode[] = ['subscription', 'api_key', 'off'];

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
	/** Why Claude itself cannot run, null when it can. `gate.reason` is about the whole worker. Absent on older workers. */
	claude_reason?: string | null;
}

export function getClaudeAuth(refresh = false): Promise<ClaudeAuthSummary> {
	return api.get(`/settings/claude-auth${refresh ? '?refresh=true' : ''}`, { cache: 'no-store' });
}

export function setClaudeAuthMode(mode: ClaudeAuthMode): Promise<ClaudeAuthSummary> {
	return api.put('/settings/claude-auth', { mode });
}
