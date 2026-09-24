/**
 * How Claude Code is paid for on this worker, and whether agents can start.
 *
 * The setup wizard and Settings talk to the backend only through this module,
 * so the pieces that are still landing on the backend stay behind one small
 * interface. Expected backend (plan items C9 and C10):
 *
 * - `GET  /api/v1/settings/claude-auth-mode` answers `{ mode }`, where `mode`
 *   is `'subscription'`, `'api_key'` or `null` (not chosen yet).
 * - `PUT  /api/v1/settings/claude-auth-mode` with `{ mode }` stores the choice
 *   (owner only) and answers `{ mode }`.
 * - `GET  /api/v1/auth/status` carries `agent_credentials`:
 *   `{ ready, mode, reason }`, the answer of `agent_credentials_ready()`.
 *
 * Until they exist, the calls fall back without failing: a 404 or 405 on the
 * mode route means "this worker has no mode setting yet" (`supported: false`),
 * and readiness is worked out here from what the worker already reports (the
 * CLI's own `claude auth status`, and whether an Anthropic API key is saved).
 * On such a worker a saved API key still takes precedence over the CLI's
 * login, which `keyOverridesSubscription` reports so the UI can say so.
 */
import { api, isApiError, type AuthStatus } from './client';

export type ClaudeAuthMode = 'subscription' | 'api_key';

export interface AuthModeState {
	/** The worker stores the mode itself. False on a worker without the setting. */
	supported: boolean;
	/** The stored choice, or null when none has been made (or none can be stored). */
	mode: ClaudeAuthMode | null;
}

export interface AgentCredentials {
	/** A new agent run can start with the chosen mode's credential. */
	ready: boolean;
	mode: ClaudeAuthMode | null;
	/** What is missing, fit to show the user, when not ready. */
	reason: string | null;
	/** `gate`: answered by the worker's own credential gate; `local`: worked out here. */
	source: 'gate' | 'local';
}

export interface AnthropicKeyState {
	set: boolean;
	/** Masked form for display, never the key. */
	masked: string;
}

const MODE_PATH = '/settings/claude-auth-mode';

export function isClaudeAuthMode(value: unknown): value is ClaudeAuthMode {
	return value === 'subscription' || value === 'api_key';
}

function missingRoute(error: unknown): boolean {
	return isApiError(error) && (error.status === 404 || error.status === 405);
}

/** The stored mode, or `supported: false` on a worker without the setting. */
export async function getClaudeAuthMode(): Promise<AuthModeState> {
	try {
		const body = await api.get<{ mode?: unknown }>(MODE_PATH, { cache: 'no-store' });
		return { supported: true, mode: isClaudeAuthMode(body?.mode) ? body.mode : null };
	} catch (error) {
		if (missingRoute(error)) return { supported: false, mode: null };
		throw error;
	}
}

/**
 * Store the mode. On a worker without the setting nothing is stored and the
 * answer says so; the wizard then keeps the choice for this visit only.
 */
export async function setClaudeAuthMode(mode: ClaudeAuthMode): Promise<AuthModeState> {
	try {
		const body = await api.put<{ mode?: unknown }>(MODE_PATH, { mode });
		return { supported: true, mode: isClaudeAuthMode(body?.mode) ? body.mode : mode };
	} catch (error) {
		if (missingRoute(error)) return { supported: false, mode };
		throw error;
	}
}

/** Whether an Anthropic API key is saved on this worker. */
export async function getAnthropicKeyState(): Promise<AnthropicKeyState> {
	const body = await api.get<{ anthropic_api_key_set?: boolean; anthropic_api_key_masked?: string }>(
		'/settings/integrations', { cache: 'no-store' });
	return { set: !!body?.anthropic_api_key_set, masked: body?.anthropic_api_key_masked || '' };
}

/**
 * Save the Anthropic API key on this worker (it stays on this worker). The
 * worker checks the key with Anthropic before storing it and refuses a key
 * Anthropic rejects.
 */
export async function saveAnthropicApiKey(apiKey: string): Promise<AnthropicKeyState> {
	const body = await api.post<{ success?: boolean; masked_key?: string }>(
		'/settings/integrations/anthropic', { api_key: apiKey });
	return { set: body?.success !== false, masked: body?.masked_key || '' };
}

interface GateAnswer { ready?: unknown; mode?: unknown; reason?: unknown }

/** Read the worker's credential gate out of an `/auth/status` answer, if it has one. */
export function gateFromStatus(status: (AuthStatus & { agent_credentials?: GateAnswer }) | null | undefined): AgentCredentials | null {
	const gate = status?.agent_credentials;
	if (!gate || typeof gate !== 'object' || typeof gate.ready !== 'boolean') return null;
	return {
		ready: gate.ready,
		mode: isClaudeAuthMode(gate.mode) ? gate.mode : null,
		reason: typeof gate.reason === 'string' && gate.reason ? gate.reason : null,
		source: 'gate',
	};
}

/**
 * Whether agents can start with `mode`. Uses the worker's gate when it has one
 * and it answers for the same mode; otherwise asks what the worker reports.
 */
export async function getAgentCredentials(mode: ClaudeAuthMode): Promise<AgentCredentials> {
	const status = await api.get<AuthStatus & { agent_credentials?: GateAnswer }>('/auth/status', { cache: 'no-store' });
	const gate = gateFromStatus(status);
	if (gate && (gate.mode === null || gate.mode === mode)) return { ...gate, mode };
	return localReadiness(mode, status);
}

async function localReadiness(mode: ClaudeAuthMode, status: AuthStatus): Promise<AgentCredentials> {
	if (mode === 'subscription') {
		const ready = status.claude_cli?.signed_in === true || status.claude_authenticated === true;
		return {
			ready, mode, source: 'local',
			reason: ready ? null : status.claude_cli?.installed === false
				? 'The Claude Code CLI is not installed on this worker yet.'
				: 'The Claude Code CLI is not signed in.',
		};
	}
	const key = await getAnthropicKeyState();
	return { ready: key.set, mode, source: 'local', reason: key.set ? null : 'No Anthropic API key is saved on this worker.' };
}

/**
 * On a worker without the mode setting, a saved API key is used instead of the
 * CLI's login. True when that applies to a subscription choice.
 */
export function keyOverridesSubscription(modeState: AuthModeState, key: AnthropicKeyState | null): boolean {
	return !modeState.supported && !!key?.set;
}
