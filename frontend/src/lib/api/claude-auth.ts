/**
 * Which credential Claude Code runs on: the user's Claude subscription (the
 * stock CLI's own sign-in), the Anthropic API key stored on this worker, a
 * cloud provider or gateway the owner set up here (Amazon Bedrock, Google
 * Vertex AI, Microsoft Foundry or an LLM gateway), or off (Claude models
 * leave the picker and Claude turns are refused).
 *
 * The mode is an explicit choice. AI Shuffle never switches it on its own,
 * whichever credentials exist.
 */

import { api, type AgentGateStatus } from './client';

export type ClaudeAuthMode = 'subscription' | 'api_key' | 'cloud' | 'off';

export const CLAUDE_AUTH_MODES: readonly ClaudeAuthMode[] = ['subscription', 'api_key', 'cloud', 'off'];

export type ClaudeCloudMethodId = 'bedrock' | 'vertex' | 'foundry' | 'gateway';

/** The cloud method's readiness. Names and presence only, never a value. */
export interface ClaudeCloudStatus {
	method: ClaudeCloudMethodId | null;
	method_label: string | null;
	configured: boolean;
	ready: boolean;
	/** Labels of the required values that are not stored. */
	missing: string[];
	/** The encrypted credential store cannot be read. */
	storage_locked: boolean;
}

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
	/** The cloud provider or gateway method. Absent on older workers. */
	cloud?: ClaudeCloudStatus;
	/** Whether new Claude agent runs can start on this worker, and why not. */
	gate?: AgentGateStatus;
	/** Why Claude itself cannot run, null when it can. `gate.reason` is about the whole worker. Absent on older workers. */
	claude_reason?: string | null;
}

export function getClaudeAuth(refresh = false): Promise<ClaudeAuthSummary> {
	return api.get(`/settings/claude-auth${refresh ? '?refresh=true' : ''}`, { cache: 'no-store' });
}

/**
 * Store the mode (owner only). Turning on `cloud` needs the warning
 * acknowledged: the server refuses it otherwise.
 */
export function setClaudeAuthMode(mode: ClaudeAuthMode, acknowledgeWarning = false): Promise<ClaudeAuthSummary> {
	return api.put('/settings/claude-auth', acknowledgeWarning ? { mode, acknowledge_warning: true } : { mode });
}

// ============================================================================
// Cloud provider or gateway (owner only)
// ============================================================================

export interface ClaudeCloudField {
	name: string;
	label: string;
	/** The environment variable the Claude CLI receives. */
	env: string;
	/** Write-only: never returned, only whether it is set. */
	secret: boolean;
	kind: 'text' | 'region' | 'url' | 'json' | 'resource';
	required: boolean;
	/** The alternative this field belongs to, when the method has alternatives. */
	choice: string | null;
}

export interface ClaudeCloudMethod {
	id: ClaudeCloudMethodId;
	label: string;
	/** The Claude Code documentation page for this method. */
	docs: string;
	/** Every variable this method can pass to the Claude CLI. */
	allowlist: string[];
	/** Alternatives the owner picks one of (Bedrock: keys or API key). */
	choices: { id: string; label: string }[];
	fields: ClaudeCloudField[];
}

export interface ClaudeCloudConfig {
	method: ClaudeCloudMethodId | null;
	/** Non-secret values only. */
	values: Record<string, string>;
	/** Whether each secret of the saved method is stored. */
	secrets: Record<string, { set: boolean }>;
	status: ClaudeCloudStatus;
	methods: ClaudeCloudMethod[];
	warning: string;
	gateway_warning: string;
}

export function getClaudeCloud(): Promise<ClaudeCloudConfig> {
	return api.get('/settings/claude-auth/cloud', { cache: 'no-store' });
}

/**
 * Save the method and its values. A secret left out keeps the stored one when
 * the method is unchanged. `activate` also switches Claude to this method.
 */
export function saveClaudeCloud(
	method: ClaudeCloudMethodId,
	values: Record<string, string>,
	options: { activate?: boolean } = {}
): Promise<ClaudeCloudConfig & { auth: ClaudeAuthSummary }> {
	return api.put('/settings/claude-auth/cloud', {
		method,
		values,
		acknowledge_warning: true,
		activate: !!options.activate
	});
}

/** Delete the method and its stored secrets. The mode stays as it is. */
export async function clearClaudeCloud(): Promise<{ success: boolean; auth: ClaudeAuthSummary }> {
	return (await api.delete<{ success: boolean; auth: ClaudeAuthSummary }>('/settings/claude-auth/cloud'))!;
}
