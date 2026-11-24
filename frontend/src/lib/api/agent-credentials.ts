/**
 * How the Claude provider is paid for on this worker, and whether agents can start.
 *
 * The setup wizard reads all of it from one call, `GET /api/v1/settings/claude-auth`
 * (the same summary Settings > Providers > Claude shows):
 *
 * - `mode`: `'subscription'`, `'cloud'` or `'off'`, with `explicit` false only before
 *   startup has recorded the worker's default.
 * - `cli`: what `claude auth status` reports, with the account only when signed in.
 * - `api_key`: whether a key is stored, and its last four characters.
 * - `gate`: the worker's credential gate, the same answer that refuses turns
 *   (`ready`, `reason`, `claude_credential_ready`). Its
 *   `ready` and `reason` cover the whole worker (any provider).
 * - `claude_reason`: why Claude itself cannot run, null when it can.
 *
 * `PUT /api/v1/settings/claude-auth` with `{ mode }` stores the choice (owner
 * only) and answers with the same summary. `POST /api/v1/settings/integrations/anthropic`
 * saves a key, which becomes the "Claude API" provider beside the subscription
 * and never changes the mode. The worker never switches modes itself.
 */
import { api } from './client';
import {
	getClaudeAuth, setClaudeAuthMode as putClaudeAuthMode,
	type ClaudeAuthMode, type ClaudeAuthSummary
} from './claude-auth';

export type { ClaudeAuthMode } from './claude-auth';

export interface AgentCredentials {
	/** The mode Claude Code runs in on this worker. */
	mode: ClaudeAuthMode;
	/** The mode's name as Settings shows it. */
	modeLabel: string;
	/** False only before the worker has recorded its default mode. */
	explicit: boolean;
	/** The server gate's `ready`: some agent provider is ready. */
	ready: boolean;
	/** The mode's Claude Code credential exists (CLI signed in, or the cloud method set up). */
	credentialReady: boolean;
	/** The saved Anthropic key's "Claude API" provider can run. */
	apiKeyReady: boolean;
	/** Why Claude cannot run, fit to show the user; null when it can. */
	reason: string | null;
	cli: ClaudeAuthSummary['cli'];
	apiKey: ClaudeAuthSummary['api_key'];
}

/** The gate's id for the saved Anthropic key's native "Claude API" provider. */
export const CLAUDE_API_READY_ID = 'gw:claude-api';

export function isClaudeAuthMode(value: unknown): value is ClaudeAuthMode {
	return value === 'subscription' || value === 'cloud' || value === 'off';
}

/** The wizard's view of the worker's `claude-auth` summary. */
export function credentialsFromSummary(summary: ClaudeAuthSummary): AgentCredentials {
	const mode: ClaudeAuthMode = isClaudeAuthMode(summary.mode) ? summary.mode : 'subscription';
	const gate = summary.gate;
	// Off: Claude is turned off on this worker, so no credential is in use.
	const credentialReady = gate
		? gate.claude_credential_ready === true
		: mode === 'subscription'
			? summary.cli.signed_in
			: mode === 'cloud'
				? !!summary.cloud?.ready
				: false;
	// The key runs on the separate "Claude API" provider, so its readiness is
	// that provider's, never the Claude Code CLI's. The subscription being off
	// (the `off` mode) does not turn it off: it has its own switch. The gate's
	// list decides, so an older worker that still stops it when off says so.
	const apiKeyReady = gate?.ready_providers
		? gate.ready_providers.includes(CLAUDE_API_READY_ID)
		: summary.api_key.set;
	return {
		mode,
		modeLabel: summary.mode_label,
		explicit: summary.explicit,
		ready: gate ? gate.ready === true : credentialReady || apiKeyReady,
		credentialReady,
		apiKeyReady,
		reason: claudeReason(summary, credentialReady),
		cli: summary.cli,
		apiKey: summary.api_key,
	};
}

/**
 * The Claude-specific reason. A worker that sends `claude_reason` decides
 * it; an older one only has the worker-wide `gate.reason`, used when Claude's
 * own credential is not ready.
 */
function claudeReason(summary: ClaudeAuthSummary, credentialReady: boolean): string | null {
	if ('claude_reason' in summary) return summary.claude_reason || null;
	return credentialReady ? null : summary.gate?.reason || null;
}

/**
 * The mode, the CLI account, the key's last four and the gate, in one call.
 * `refresh` re-runs `claude auth status` instead of using the worker's cache.
 */
export async function getAgentCredentials(refresh = false): Promise<AgentCredentials> {
	return credentialsFromSummary(await getClaudeAuth(refresh));
}

/** Store the mode (owner only). Takes effect for the next agent run. */
export async function setClaudeAuthMode(mode: ClaudeAuthMode): Promise<AgentCredentials> {
	return credentialsFromSummary(await putClaudeAuthMode(mode));
}

/**
 * Save the Anthropic API key on this worker (it stays on this worker). The
 * worker checks the key with Anthropic first and refuses a key Anthropic
 * rejects. The key becomes the "Claude API" provider; saving it never
 * changes the Claude mode.
 */
export async function saveAnthropicApiKey(apiKey: string): Promise<{ masked: string }> {
	const body = await api.post<{ success?: boolean; masked_key?: string }>(
		'/settings/integrations/anthropic', { api_key: apiKey });
	return { masked: body?.masked_key || '' };
}

/** "Key ending in abcd", as Settings > Providers shows it. */
export function keyLabel(key: AgentCredentials['apiKey']): string {
	if (!key.set) return 'No key stored';
	return key.last4 ? `Key ending in ${key.last4}` : 'Key stored';
}
