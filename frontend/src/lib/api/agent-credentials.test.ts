import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('./client', async (importOriginal) => ({
	...(await importOriginal<typeof import('./client')>()),
	api: { get: mocks.get, put: mocks.put, post: mocks.post },
}));
import {
	getAgentCredentials, getClaudeAuthMode, keyOverridesSubscription, saveAnthropicApiKey, setClaudeAuthMode
} from './agent-credentials';

const notFound = { status: 404, detail: 'Not Found' };

beforeEach(() => vi.resetAllMocks());

describe('Claude auth mode', () => {
	it('reads and stores the mode where the worker has the setting', async () => {
		mocks.get.mockResolvedValue({ mode: 'api_key' });
		expect(await getClaudeAuthMode()).toEqual({ supported: true, mode: 'api_key' });
		mocks.put.mockResolvedValue({ mode: 'subscription' });
		expect(await setClaudeAuthMode('subscription')).toEqual({ supported: true, mode: 'subscription' });
		expect(mocks.put).toHaveBeenCalledWith('/settings/claude-auth-mode', { mode: 'subscription' });
	});

	it('falls back without failing on a worker without the setting', async () => {
		mocks.get.mockRejectedValue(notFound);
		expect(await getClaudeAuthMode()).toEqual({ supported: false, mode: null });
		mocks.put.mockRejectedValue({ status: 405, detail: 'Method Not Allowed' });
		expect(await setClaudeAuthMode('api_key')).toEqual({ supported: false, mode: 'api_key' });
	});

	it('passes other errors on', async () => {
		mocks.put.mockRejectedValue({ status: 403, detail: 'Owner access required' });
		await expect(setClaudeAuthMode('api_key')).rejects.toMatchObject({ status: 403 });
	});

	it('treats an unknown stored value as no choice', async () => {
		mocks.get.mockResolvedValue({ mode: 'pooled' });
		expect(await getClaudeAuthMode()).toEqual({ supported: true, mode: null });
	});
});

describe('agent credentials', () => {
	it('uses the worker gate when it answers for the chosen mode', async () => {
		mocks.get.mockResolvedValue({ claude_authenticated: true, agent_credentials: { ready: false, mode: 'api_key', reason: 'No key' } });
		expect(await getAgentCredentials('api_key')).toEqual({ ready: false, mode: 'api_key', reason: 'No key', source: 'gate' });
		expect(mocks.get).toHaveBeenCalledOnce();
	});

	it('works it out from the CLI status on a worker without the gate', async () => {
		mocks.get.mockResolvedValue({ claude_authenticated: false, claude_cli: { installed: true, signed_in: false } });
		expect(await getAgentCredentials('subscription')).toMatchObject({ ready: false, source: 'local', reason: 'The Claude Code CLI is not signed in.' });
		mocks.get.mockResolvedValue({ claude_authenticated: true, claude_cli: { installed: true, signed_in: true } });
		expect(await getAgentCredentials('subscription')).toMatchObject({ ready: true, reason: null });
	});

	it('ignores a gate that answers for the other mode and checks the saved key', async () => {
		mocks.get.mockImplementation(async (path: string) => path === '/auth/status'
			? { claude_authenticated: true, agent_credentials: { ready: true, mode: 'subscription', reason: null } }
			: { anthropic_api_key_set: true, anthropic_api_key_masked: 'sk-ant-...abcd' });
		expect(await getAgentCredentials('api_key')).toEqual({ ready: true, mode: 'api_key', reason: null, source: 'local' });
	});

	it('saves a key through the integrations route', async () => {
		mocks.post.mockResolvedValue({ success: true, masked_key: 'sk-ant-...abcd' });
		expect(await saveAnthropicApiKey('sk-ant-secret')).toEqual({ set: true, masked: 'sk-ant-...abcd' });
		expect(mocks.post).toHaveBeenCalledWith('/settings/integrations/anthropic', { api_key: 'sk-ant-secret' });
	});

	it('reports that a saved key wins over the CLI only where the mode setting is missing', () => {
		const key = { set: true, masked: '' };
		expect(keyOverridesSubscription({ supported: false, mode: 'subscription' }, key)).toBe(true);
		expect(keyOverridesSubscription({ supported: true, mode: 'subscription' }, key)).toBe(false);
		expect(keyOverridesSubscription({ supported: false, mode: 'subscription' }, { set: false, masked: '' })).toBe(false);
	});
});
