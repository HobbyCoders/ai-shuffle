/**
 * Settings > Providers > Claude: its credentials, each with its own switch
 * like ChatGPT and the OpenAI API key. The CLI account and the key's last
 * four, the warning when the subscription has no credential, the
 * subscription switch (Claude API keeps running), and read-only.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: mocks.checkAuth }
}));

import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { claudeAccounts, claudeSummary, subAccount } from './fixtures';
import { fakeWorker } from './fake-worker';
import { CLAUDE_SUB_ITEM } from './providers-model';
import { terminals } from '$lib/stores/terminals.svelte';
import { layout } from '$lib/stores/layout.svelte';

let worker: ReturnType<typeof fakeWorker>;

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function show(options: { readOnly?: boolean } = {}) {
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: 'provider:claude' });
	const context = options.readOnly ? new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) : undefined;
	render(ProvidersCollection, context ? { context } : {});
	await flush();
}

const subSwitch = () => within(credentials()).getByRole('switch', { name: /Claude Max subscription/ });
const credentials = () => screen.getByRole('region', { name: /^Credentials/ });

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
	terminals.reset();
	layout.reset();
	worker = fakeWorker({
		claude: claudeSummary({ api_key: { set: true, last4: 'WXYZ' } }),
		integrations: {
			anthropic_api_key_set: true,
			anthropic_api_key_masked: null,
			fal_api_key_set: false,
			fal_api_key_masked: '',
			fal_enabled: true
		}
	});
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('shows the subscription switch, the CLI account and the key’s last four, and no mode picker', async () => {
	await show();
	expect(screen.getByRole('heading', { level: 2, name: 'Claude' })).toBeTruthy();
	expect(screen.getByText('Runs on subscription')).toBeTruthy();
	expect(screen.queryByRole('group', { name: 'Runs on' })).toBeNull();
	expect(subSwitch().getAttribute('aria-checked')).toBe('true');

	const creds = credentials();
	expect(within(creds).getByText('Claude Max subscription')).toBeTruthy();
	// Its switch (on, above) says it is in use, like every other provider's row.
	expect(within(creds).getByText('Anthropic API key')).toBeTruthy();
	// The key is the Claude API provider beside the subscription, not a standby.
	expect(within(creds).getByText('Active for Claude API')).toBeTruthy();
	// Older workers list no saved keys: that single key row has nothing to switch.
	expect(within(creds).getAllByRole('switch')).toHaveLength(1);

	// The list rows carry the account and the key's last four.
	expect(screen.getByText('owner@example.com, Max plan')).toBeTruthy();
	expect(screen.getByText('sk-ant-…WXYZ, active for Claude API')).toBeTruthy();
	expect(screen.queryByRole('alert')).toBeNull();
});

it('warns when the active mode has no credential', async () => {
	// A saved sign-in that has lapsed: Claude is in use, but cannot run.
	const summary = claudeSummary({
		api_key: { set: false, last4: null },
		accounts: claudeAccounts({ subscription: [subAccount({ signed_in: false, needs_sign_in: true })] }),
		gate: {
			ready: false,
			mode: 'subscription',
			claude_credential_ready: false,
			reason: 'The Claude Code CLI is not signed in.'
		}
	});
	summary.cli = { ...summary.cli, signed_in: false, email: null, org_name: null, plan: null };
	worker.state.claude = summary;
	worker.state.integrations.anthropic_api_key_set = false;
	await show();
	expect(screen.getByText('Not ready')).toBeTruthy();
	expect(screen.getByText('Claude cannot run: the Claude Code CLI is not signed in on this worker.')).toBeTruthy();
	// No key is saved, so there is no key row to warn about.
	expect(within(credentials()).queryByText('Anthropic API key')).toBeNull();
});

it('keeps Claude API running on the saved key while the subscription is signed out', async () => {
	const summary = claudeSummary({
		api_key: { set: true, last4: 'WXYZ' },
		gate: { ready: true, mode: 'subscription', claude_credential_ready: false, reason: null }
	});
	summary.cli = { ...summary.cli, signed_in: false, email: null, org_name: null, plan: null };
	worker.state.claude = summary;
	await show();
	expect(screen.getByText('Not signed in · API key saved')).toBeTruthy();
	expect(screen.getByText(/Claude subscription cannot run: the Claude Code CLI is not signed in on this worker\. Claude API models run on the saved key\./)).toBeTruthy();
});

it('turns the subscription back on from its switch', async () => {
	worker.state.claude = claudeSummary({ ...worker.state.claude, mode: 'off' });
	await show();
	expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	expect(subSwitch().getAttribute('aria-checked')).toBe('false');
	await fireEvent.click(subSwitch());
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(1));
	expect(worker.sent('PUT', '/settings/claude-auth')[0].body).toEqual({ mode: 'subscription' });
	await waitFor(() => expect(subSwitch().getAttribute('aria-checked')).toBe('true'));
	await waitFor(() => expect(within(credentials()).getByText('Active for Claude API')).toBeTruthy());
	expect(mocks.hydrate).toHaveBeenCalled();
});

it('turns the subscription off and keeps Claude API on', async () => {
	await show();
	await fireEvent.click(subSwitch());
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth')[0]?.body).toEqual({ mode: 'off' }));
	await waitFor(() => expect(screen.getByText('Subscription off · API key on')).toBeTruthy());
	expect(within(credentials()).getByText('Active for Claude API')).toBeTruthy();
	expect(subSwitch().getAttribute('aria-checked')).toBe('false');
});

it('is read-only for other devices', async () => {
	await show({ readOnly: true });
	expect((subSwitch() as HTMLButtonElement).disabled).toBe(true);
	await fireEvent.click(subSwitch());
	await flush();
	expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
});

it('manages an installed CLI without a Claude sign-in', async () => {
	const signedOut = claudeSummary({ api_key: { set: false, last4: null } });
	signedOut.cli = { ...signedOut.cli, signed_in: false, email: null, org_name: null, plan: null };
	worker = fakeWorker({
		claude: signedOut,
		integrations: { anthropic_api_key_set: false, anthropic_api_key_masked: null, fal_api_key_set: false,
			fal_api_key_masked: '', fal_enabled: true }
	});
	vi.stubGlobal('fetch', worker.fetch);
	await show();
	expect(screen.getByRole('region', { name: 'Connect' })).toBeTruthy();
	expect(await screen.findByRole('switch', { name: 'Check for updates at startup' })).toBeTruthy();
	expect(screen.getByText('Version')).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Remove' })).toBeTruthy();
	expect(within(screen.getByRole('region', { name: 'Connect' })).getAllByRole('button', { name: 'Set up' }).length).toBeGreaterThan(0);
});

it('offers only setup when Claude is not connected and the CLI is absent', async () => {
	const absent = claudeSummary({ api_key: { set: false, last4: null } });
	absent.cli = { ...absent.cli, installed: false, signed_in: false, email: null, org_name: null, plan: null };
	worker = fakeWorker({
		claude: absent,
		integrations: { anthropic_api_key_set: false, anthropic_api_key_masked: null, fal_api_key_set: false,
			fal_api_key_masked: '', fal_enabled: true }
	});
	vi.stubGlobal('fetch', worker.fetch);
	await show();
	expect(screen.getByRole('region', { name: 'Connect' })).toBeTruthy();
	expect(screen.queryByRole('switch', { name: 'Check for updates at startup' })).toBeNull();
	expect(screen.queryByRole('button', { name: 'Install Claude Code' })).toBeNull();
});

const terminalTargets = {
	worker: { id: 'w', name: 'ServerWorker', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
	shells: [{ name: 'bash', default: true }], default_shell: 'bash',
	default_folder: { path: '/workspace', source: 'workspace' }, limit: 8, open: 0
};

it('opens the chatless Terminal page from the subscription when the worker allows shells', async () => {
	worker = fakeWorker({ claude: claudeSummary() }, { 'GET /terminals/targets': () => terminalTargets });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: CLAUDE_SUB_ITEM });
	render(ProvidersCollection);
	await flush(10);

	// The worker-wide probe: no chat id on the request.
	expect(worker.calls.some((c) => c.method === 'GET' && c.path === '/terminals/targets')).toBe(true);
	await fireEvent.click(await screen.findByRole('button', { name: 'Open terminal' }));
	expect(layout.current.activeView?.type).toBe('terminal');
});

it('offers no Open terminal when the worker refuses terminals', async () => {
	worker = fakeWorker({ claude: claudeSummary() }, {
		'GET /terminals/targets': () => { throw { status: 403, detail: 'Only the owner can use terminals.' }; }
	});
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: CLAUDE_SUB_ITEM });
	render(ProvidersCollection);
	await flush(10);

	expect(terminals.capability.status).toBe('denied');
	expect(screen.queryByRole('button', { name: 'Open terminal' })).toBeNull();
});

it('carries the Claude compaction switch, on by default, and saves a change', async () => {
	await show();
	const group = await screen.findByRole('region', { name: 'Chats' });
	const sw = await within(group).findByRole('switch', { name: /Smarter compaction/ });
	expect(sw.getAttribute('aria-checked')).toBe('true');
	await fireEvent.click(sw);
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-compaction')[0]?.body).toEqual({ enabled: false }));
});

it('lists saved sign-ins with "Another Subscription"; the active one\'s switch turns the subscription off', async () => {
	worker.state.claude = claudeSummary({
		api_key: { set: false, last4: null },
		accounts: claudeAccounts({
			default_in_use: false,
			subscription: [subAccount({ active: false }), subAccount({ id: 'work', kind: 'saved', label: 'Work', email: 'work@example.com', plan: 'pro', active: true })]
		})
	});
	worker.state.integrations.anthropic_api_key_set = false;
	await show();
	const creds = credentials();
	expect(within(creds).getByText('Personal')).toBeTruthy();
	expect(within(creds).getByText('Work')).toBeTruthy();
	expect(within(creds).getByRole('switch', { name: 'Use Personal' }).getAttribute('aria-checked')).toBe('false');
	expect(within(creds).getByText('Active')).toBeTruthy();
	const add = screen.getByRole('region', { name: 'Add' });
	expect(within(add).getByText('Another Subscription')).toBeTruthy();
	expect(within(add).getByText('Becomes active after sign-in')).toBeTruthy();
	// Off keeps the selected sign-in, so turning the subscription back on resumes it.
	await fireEvent.click(within(creds).getByRole('switch', { name: 'Work on' }));
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(1));
	expect(worker.sent('PUT', '/settings/claude-auth')[0].body).toEqual({ mode: 'off' });
	expect(worker.state.claude.accounts?.subscription.map((a) => a.active)).toEqual([false, true]);
	expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/work/activate')).toHaveLength(0);
});

it('opens the active saved sign-in from the stable subscription link', async () => {
	worker.state.claude = claudeSummary({
		accounts: claudeAccounts({
			default_in_use: false,
			subscription: [subAccount({ active: false }), subAccount({ id: 'work', kind: 'saved', label: 'Work', active: true })]
		})
	});
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: CLAUDE_SUB_ITEM });
	render(ProvidersCollection);
	await flush();
	expect(await screen.findByRole('heading', { level: 2, name: 'Work' })).toBeTruthy();
});
