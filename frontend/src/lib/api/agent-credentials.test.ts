import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('./client', async (importOriginal) => ({
	...(await importOriginal<typeof import('./client')>()),
	api: { get: mocks.get, put: mocks.put, post: mocks.post },
}));
import {
	credentialsFromSummary, getAgentCredentials, keyLabel, saveAnthropicApiKey, setClaudeAuthMode
} from './agent-credentials';
import type { ClaudeAuthSummary } from './claude-auth';

const NOT_SIGNED_IN = 'Agents cannot start: Claude Code is set to subscription mode and the Claude Code CLI is not signed in.';

/** What `GET /settings/claude-auth` answers (app/api/settings.py `claude_auth_payload`). */
function summary(over: Partial<ClaudeAuthSummary> = {}): ClaudeAuthSummary {
	return {
		mode: 'subscription',
		mode_label: 'Claude subscription',
		explicit: true,
		modes: [{ value: 'subscription', label: 'Claude subscription' }, { value: 'cloud', label: 'Cloud provider or gateway' }],
		cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
		api_key: { set: false, last4: null },
		gate: {
			ready: false, reason: NOT_SIGNED_IN, mode: 'subscription', claude_credential_ready: false, startup_state: 'setup_required',
		},
		...over,
	};
}

beforeEach(() => vi.resetAllMocks());

describe('agent credentials', () => {
	it('reads the mode, account, key and gate from one call', async () => {
		mocks.get.mockResolvedValue(summary());
		expect(await getAgentCredentials()).toEqual({
			mode: 'subscription', modeLabel: 'Claude subscription', explicit: true,
			ready: false, credentialReady: false, apiKeyReady: false, reason: NOT_SIGNED_IN,
			cli: summary().cli, apiKey: { set: false, last4: null },
		});
		expect(mocks.get).toHaveBeenCalledOnce();
		expect(mocks.get).toHaveBeenCalledWith('/settings/claude-auth', { cache: 'no-store' });
	});

	it('with the subscription off, keeps Claude API on its own switch', () => {
		const creds = credentialsFromSummary(summary({ mode: 'off', api_key: { set: true, last4: 'abcd' }, gate: undefined }));
		expect(creds.mode).toBe('off');
		expect(creds.credentialReady).toBe(false);
		expect(creds.apiKeyReady).toBe(true);
		expect(creds.ready).toBe(true);
		const none = credentialsFromSummary(summary({ mode: 'off', api_key: { set: false, last4: null }, gate: undefined }));
		expect(none.apiKeyReady).toBe(false);
		expect(none.ready).toBe(false);
	});

	it('trusts the gate when the worker lists its ready providers', () => {
		// An older worker still stops Claude API with the subscription: its list says so.
		const older = summary({ mode: 'off', api_key: { set: true, last4: 'abcd' } });
		older.gate = { ...older.gate!, ready: false, ready_providers: [] } as typeof older.gate;
		expect(credentialsFromSummary(older).apiKeyReady).toBe(false);
	});

	it('asks the worker to re-run the CLI check on refresh', async () => {
		mocks.get.mockResolvedValue(summary());
		await getAgentCredentials(true);
		expect(mocks.get).toHaveBeenCalledWith('/settings/claude-auth?refresh=true', { cache: 'no-store' });
	});

	it('reports a signed-in CLI account as ready', async () => {
		mocks.get.mockResolvedValue(summary({
			cli: { installed: true, signed_in: true, email: 'owner@example.com', org_name: null, plan: 'max', auth_method_label: null, error: null },
			gate: { ready: true, reason: null, mode: 'subscription', claude_credential_ready: true, startup_state: 'ready' },
		}));
		expect(await getAgentCredentials()).toMatchObject({ ready: true, credentialReady: true, reason: null, cli: { email: 'owner@example.com' } });
	});

	it('uses the Claude reason, not the worker-wide one, when the worker sends it', () => {
		const claudeReason = 'Claude cannot run: the Claude Code CLI is not signed in on this worker.';
		// Only OpenAI is connected: the worker can run, Claude cannot.
		const openaiOnly = credentialsFromSummary(summary({
			gate: { ready: true, reason: null, mode: 'subscription', claude_credential_ready: false,
				startup_state: 'ready', any_provider_ready: true, ready_providers: ['openai'] },
			claude_reason: claudeReason,
		}));
		expect(openaiOnly).toMatchObject({ ready: true, credentialReady: false, reason: claudeReason });
		// Nothing connected: the worker-wide reason is not shown as Claude's.
		const none = credentialsFromSummary(summary({
			gate: { ready: false, reason: 'Connect an AI provider to start agents.', mode: 'subscription', claude_credential_ready: false, startup_state: 'setup_required', any_provider_ready: false, ready_providers: [] },
			claude_reason: claudeReason,
		}));
		expect(none.reason).toBe(claudeReason);
		// Claude ready while the worker-wide reason names something else.
		const claudeOk = credentialsFromSummary(summary({
			gate: { ready: true, reason: 'Something about another provider.', mode: 'subscription', claude_credential_ready: true, startup_state: 'ready' },
			claude_reason: null,
		}));
		expect(claudeOk.reason).toBeNull();
	});

	it('reads the key as ready from the Claude API provider, beside the subscription', () => {
		const gate = { ready: true, reason: null, mode: 'subscription' as const, claude_credential_ready: false, startup_state: 'ready' as const };
		// The key runs natively while this worker's CLI is not signed in.
		expect(credentialsFromSummary(summary({
			api_key: { set: true, last4: 'abcd' }, gate: { ...gate, ready_providers: ['gw:claude-api'] },
		}))).toMatchObject({ apiKeyReady: true, credentialReady: false });
		// The CLI sign-in never stands in for the key.
		expect(credentialsFromSummary(summary({
			gate: { ...gate, claude_credential_ready: true, ready_providers: ['claude'] },
		}))).toMatchObject({ apiKeyReady: false, credentialReady: true });
	});

	it('falls back to the gate reason on an older worker only while Claude is not ready', () => {
		expect(credentialsFromSummary(summary()).reason).toBe(NOT_SIGNED_IN);
		expect(credentialsFromSummary(summary({
			gate: { ready: true, reason: 'Worker-wide note.', mode: 'subscription', claude_credential_ready: true },
		})).reason).toBeNull();
	});

	it('stores the mode and answers with the new summary', async () => {
		mocks.put.mockResolvedValue(summary({ mode: 'cloud', mode_label: 'Cloud provider or gateway' }));
		expect(await setClaudeAuthMode('cloud')).toMatchObject({ mode: 'cloud', modeLabel: 'Cloud provider or gateway' });
		expect(mocks.put).toHaveBeenCalledWith('/settings/claude-auth', { mode: 'cloud' });
	});

	it('passes errors on', async () => {
		mocks.put.mockRejectedValue({ status: 403, detail: 'Owner access required' });
		await expect(setClaudeAuthMode('off')).rejects.toMatchObject({ status: 403 });
		mocks.get.mockRejectedValue({ status: 404, detail: 'Not Found' });
		await expect(getAgentCredentials()).rejects.toMatchObject({ status: 404 });
	});

	it('saves a key without touching the Claude mode', async () => {
		mocks.post.mockResolvedValue({ success: true, masked_key: 'sk-ant-...abcd' });
		expect(await saveAnthropicApiKey('sk-ant-secret')).toEqual({ masked: 'sk-ant-...abcd' });
		expect(mocks.post).toHaveBeenCalledWith('/settings/integrations/anthropic', { api_key: 'sk-ant-secret' });
	});

	it('reads a legacy API key mode as the subscription', async () => {
		// Older workers may still answer with the removed mode.
		mocks.get.mockResolvedValue(summary({ mode: 'api_key' as unknown as ClaudeAuthSummary['mode'] }));
		expect((await getAgentCredentials()).mode).toBe('subscription');
	});

	it('names a stored key by its last four, as Settings does', () => {
		expect(keyLabel({ set: true, last4: 'abcd' })).toBe('Key ending in abcd');
		expect(keyLabel({ set: true, last4: null })).toBe('Key stored');
		expect(keyLabel({ set: false, last4: null })).toBe('No key stored');
	});
});
