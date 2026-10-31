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

export interface ClaudeKeyAccount {
	id: string;
	label: string;
	active: boolean;
	masked_key: string;
	last4: string | null;
}

/**
 * A Claude Code CLI sign-in saved on this worker (never synced to others).
 * `default` is Claude Code's own folder; `saved` is an extra sign-in in its
 * own folder. Identity is what `claude auth status` last reported for it.
 */
export interface ClaudeSubscriptionAccount {
	id: string;
	label: string;
	kind: 'default' | 'saved';
	/** The sign-in Claude Code uses on this worker; at most one. */
	active: boolean;
	email: string | null;
	plan: string | null;
	org_name: string | null;
	signed_in: boolean | null;
	/** Unix seconds of the last `claude auth status` for this entry. */
	checked_at: number | null;
	/** The last check found no sign-in, or its folder is missing. */
	needs_sign_in: boolean;
}

/** A pending "add another" sign-in into a fresh folder. */
export interface ClaudeSubscriptionLogin {
	id: string;
	status: 'running' | 'waiting' | 'failed';
	error: string | null;
	/** True: drive it through the in-app terminal (`/auth/claude/cli-login*`). */
	terminal: boolean;
	command: string;
	env: { CLAUDE_CONFIG_DIR: string };
	/** Without the in-app terminal: what to run in a terminal on that worker. */
	commands: { powershell: string; cmd: string; posix: string } | null;
	/** The in-app terminal's state for this sign-in (terminal workers only). */
	terminal_state?: {
		running: boolean;
		url: string | null;
		output: string;
		offset: number;
		succeeded: boolean | null;
	} | null;
}

export interface ClaudeAccounts {
	api_key: ClaudeKeyAccount[];
	subscription: ClaudeSubscriptionAccount[];
	/** False on macOS, with a plain-language `reason`. */
	subscription_support: { supported: boolean; reason: string | null };
	/** Claude Code runs in its own folder (no saved extra sign-in active). Absent on older workers. */
	default_in_use?: boolean;
	storage_locked: boolean;
	error: string | null;
	login_pending: boolean;
	/** The pending add-another sign-in, or null. Absent on older workers. */
	login?: ClaudeSubscriptionLogin | null;
	/** The active saved sign-in needs signing in again. */
	recovery_required: boolean;
}

export interface ClaudeAccountsResponse {
	accounts: ClaudeAccounts;
	account_id?: string;
	login?: ClaudeSubscriptionLogin | null;
}

/** List saved accounts; also finishes a pending add-another once the CLI reports it. */
export function getClaudeAccounts(): Promise<ClaudeAccountsResponse> {
	return api.get('/settings/claude-auth/accounts', { cache: 'no-store' });
}

export function saveCurrentClaudeSubscription(label = ''): Promise<ClaudeAccountsResponse> {
	return api.post('/settings/claude-auth/accounts/subscription/save-current', { label });
}

export function addAnotherClaudeSubscription(): Promise<ClaudeAccountsResponse> {
	return api.post('/settings/claude-auth/accounts/subscription/add-another');
}

export function cancelClaudeSubscriptionLogin(): Promise<ClaudeAccountsResponse> {
	return api.post('/settings/claude-auth/accounts/subscription/cancel-login');
}

export function renameClaudeSubscription(id: string, label: string): Promise<ClaudeAccountsResponse> {
	return api.patch(`/settings/claude-auth/accounts/subscription/${id}`, { label });
}

export function activateClaudeSubscription(id: string): Promise<ClaudeAccountsResponse> {
	return api.post(`/settings/claude-auth/accounts/subscription/${id}/activate`);
}

export async function deleteClaudeSubscription(id: string): Promise<ClaudeAccountsResponse> {
	const result = await api.delete<ClaudeAccountsResponse>(`/settings/claude-auth/accounts/subscription/${id}`);
	if (!result) throw new Error('Could not reload saved accounts');
	return result;
}

export function addClaudeKey(api_key: string, label?: string): Promise<ClaudeAccountsResponse> {
	return api.post('/settings/claude-auth/accounts/api-keys', { api_key, label, activate: true });
}

export function renameClaudeKey(id: string, label: string): Promise<ClaudeAccountsResponse> {
	return api.patch(`/settings/claude-auth/accounts/api_key/${id}`, { label });
}

export function activateClaudeKey(id: string): Promise<ClaudeAccountsResponse> {
	return api.post(`/settings/claude-auth/accounts/api_key/${id}/activate`);
}

export async function deleteClaudeKey(id: string): Promise<ClaudeAccountsResponse> {
	const result = await api.delete<ClaudeAccountsResponse>(`/settings/claude-auth/accounts/api_key/${id}`);
	if (!result) throw new Error('Could not reload saved accounts');
	return result;
}

export interface ClaudeAuthSummary {
	/** Saved API keys; absent on older workers. Independent of the Claude Code mode. */
	accounts?: ClaudeAccounts;
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
