import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { ClaudeAccounts } from '$lib/api/claude-auth';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (original) => ({
	...(await original<typeof import('$lib/constants/models.svelte')>()), hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/auth.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/auth.svelte')>()), auth: { checkAuth: mocks.checkAuth }
}));
import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { chatgptRow, claudeSummary, row } from './fixtures';
import { fakeWorker } from './fake-worker';

const accounts = (): ClaudeAccounts => ({
	api_key: [
		{ id: 'personal', label: 'Personal', active: false, masked_key: 'sk-ant-…1111', last4: '1111' },
		{ id: 'work', label: 'Work', active: true, masked_key: 'sk-ant-…2222', last4: '2222' }
	],
	subscription: [], subscription_support: { supported: false, reason: 'Single sign-in' },
	storage_locked: false, error: null, login_pending: false, recovery_required: false
});
let worker: ReturnType<typeof fakeWorker>;
const group = (name: string) => within(screen.getByRole('group', { name }));
async function show(readOnly = false) {
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	render(ProvidersCollection, { context: new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly }]]) });
	await screen.findByRole('button', { name: 'Add credential to OpenAI' });
}
beforeEach(() => {
	vi.clearAllMocks();
	providers.clear(); settingsNav.reset(); providerDrafts.list = []; providerDrafts.gateways = [];
	worker = fakeWorker({
		rows: [row({ id: 'personal', name: 'Personal key' }), row({ id: 'work', name: 'Work key', enabled: false }), chatgptRow()],
		claude: claudeSummary({ accounts: accounts() })
	});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('saved account controls', () => {
	it('switches a key with activate, refetches siblings, and leaves the subscription on', async () => {
		await show();
		await fireEvent.click(group('OpenAI').getByRole('switch', { name: 'Use Work key' }));
		await waitFor(() => expect(group('OpenAI').getByRole('button', { name: 'Work key, Active' })).toBeTruthy());
		expect(group('OpenAI').getByRole('switch', { name: 'Use Personal key' }).getAttribute('aria-checked')).toBe('false');
		expect(group('OpenAI').getByRole('switch', { name: 'OpenAI subscription on' }).getAttribute('aria-checked')).toBe('true');
		expect(worker.sent('POST', '/model-providers/work/activate')).toHaveLength(1);
		expect(worker.sent('GET', '/model-providers').length).toBeGreaterThan(1);
		expect(mocks.hydrate).toHaveBeenCalled();
		await fireEvent.click(group('OpenAI').getByRole('switch', { name: 'Work key on' }));
		await waitFor(() => expect(providers.current.providers.filter((r) => r.provider_type === 'openai').every((r) => !r.enabled)).toBe(true));
	});

	it('selects a saved Anthropic key without changing the Claude Code mode', async () => {
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Use Personal' }));
		await waitFor(() => expect(providers.current.claude?.accounts?.api_key.map((a) => a.active)).toEqual([true, false]));
		expect(providers.current.claude?.mode).toBe('subscription');
		expect(group('Claude').getByRole('button', { name: 'Use Work' })).toBeTruthy();
		expect(worker.sent('POST', '/settings/claude-auth/accounts/api_key/personal/activate')).toHaveLength(1);
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	});

	it('renames and deletes only the selected Anthropic account, without selecting a sibling', async () => {
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Work, Selected for API key mode' }));
		const name = await screen.findByRole('textbox', { name: 'Name' });
		await fireEvent.focus(name); await fireEvent.input(name, { target: { value: 'Team' } }); await fireEvent.blur(name);
		await waitFor(() => expect(worker.sent('PATCH', '/settings/claude-auth/accounts/api_key/work')[0]?.body).toEqual({ label: 'Team' }));
		await screen.findByRole('heading', { name: 'Team' });
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(providers.current.claude?.accounts?.api_key.map((a) => [a.id, a.active])).toEqual([['personal', false]]));
		expect(providers.current.claude?.mode).toBe('subscription');
	});

	it('adds another named Anthropic key and selects it without changing mode', async () => {
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Add credential to Claude' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Another API key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'Name' }), { target: { value: 'Travel' } });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'sk-ant-test-not-real-3333' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Use this key' }));
		await screen.findByRole('heading', { name: 'Travel' });
		expect(worker.sent('POST', '/settings/claude-auth/accounts/api-keys')[0].body).toEqual({ api_key: 'sk-ant-test-not-real-3333', label: 'Travel', activate: true });
		expect(providers.current.claude?.accounts?.api_key.filter((a) => a.active).map((a) => a.label)).toEqual(['Travel']);
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	});

	it('opens a second ChatGPT sign-in and keeps the old subscription active while signing in', async () => {
		worker = fakeWorker({ rows: [chatgptRow()] }, {
			'POST /model-providers': (body) => { const r = chatgptRow({ ...body as object, id: 'draft-sub', enabled: false, codex_access_token_set: false }); worker.state.rows.push(r); return r; },
			'POST /model-providers/:id/codex/device/start': () => ({ user_code: 'TEST-CODE', verification_uri: 'https://example.test/sign-in' })
		});
		await show();
		await fireEvent.click(group('OpenAI').getByRole('button', { name: 'Add credential to OpenAI' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Another Subscription/ }));
		expect(screen.getByText(/Becomes active after sign-in/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Start sign-in' }));
		await screen.findByText('TEST-CODE');
		expect(worker.sent('POST', '/model-providers')[0].body).toMatchObject({ enabled: false });
		expect(worker.state.rows.find((r) => r.id === 'chatgpt-1')?.enabled).toBe(true);
	});

	it('disables saved-key selection for read-only Settings', async () => {
		await show(true);
		expect((group('Claude').getByRole('button', { name: 'Use Personal' }) as HTMLButtonElement).disabled).toBe(true);
	});
});
