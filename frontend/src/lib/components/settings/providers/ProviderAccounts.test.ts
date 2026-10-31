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
import { chatgptRow, claudeAccounts, claudeSummary, row, subAccount } from './fixtures';
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

describe('saved Claude subscriptions', () => {
	const runningTerminal = () => ({
		supported: true, active: true, running: true, exit_code: null, succeeded: null,
		output: '', offset: 17, reset: false, url: 'https://claude.ai/oauth/authorize?test', error: null
	});

	it('adds another subscription: saves the default first, shows the in-app terminal, and Cancel stops it', async () => {
		worker = fakeWorker({ rows: [chatgptRow()], claude: claudeSummary({ accounts: claudeAccounts() }) }, {
			'GET /auth/claude/cli-login': runningTerminal
		});
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Add credential to Claude' }));
		const item = screen.getByRole('menuitem', { name: /Another Subscription/ });
		expect(item.textContent).toContain('Becomes active after sign-in');
		await fireEvent.click(item);
		await fireEvent.click(await screen.findByRole('button', { name: 'Start sign-in' }));
		await screen.findByTestId('claude-cli-terminal');
		expect(screen.getByRole('link', { name: /Anthropic’s sign-in page/ }).getAttribute('href')).toBe('https://claude.ai/oauth/authorize?test');
		// The CLI's own sign-in is saved first, so it stays listed once the new one is active.
		expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/save-current')).toHaveLength(1);
		expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/add-another')).toHaveLength(1);
		expect(worker.sent('POST', '/auth/claude/cli-login/start')).toHaveLength(0);
		expect(providers.current.claude?.accounts?.subscription.map((a) => [a.kind, a.label])).toEqual([['default', 'owner@example.com']]);
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/cancel-login').length).toBeGreaterThan(0));
		await waitFor(() => expect(providerDrafts.list).toHaveLength(0));
		expect(worker.state.claude.accounts?.login).toBeNull();
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	});

	it('without the in-app terminal shows the commands, polls, and opens the new active sign-in', async () => {
		worker = fakeWorker({
			rows: [chatgptRow()], subscriptionTerminal: false,
			claude: claudeSummary({ accounts: claudeAccounts({ subscription: [subAccount()] }) })
		});
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Add credential to Claude' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Another Subscription/ }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Start sign-in' }));
		await screen.findByTestId('claude-add-subscription');
		expect(screen.getByText(/CLAUDE_CONFIG_DIR/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'PowerShell' })).toBeTruthy();
		expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/save-current')).toHaveLength(0);
		// The owner signs in on their own terminal; the worker saves it as the active entry.
		const accounts = worker.state.claude.accounts!;
		accounts.subscription = [
			{ ...accounts.subscription[0], active: false },
			subAccount({ id: 'sub-new', kind: 'saved', label: 'work@example.com', email: 'work@example.com', plan: 'pro', active: true })
		];
		accounts.default_in_use = false;
		accounts.login = null;
		accounts.login_pending = false;
		await screen.findByRole('heading', { name: 'work@example.com' }, { timeout: 5000 });
		expect(settingsNav.selectedItem('providers')).toBe('claude:subscription:sub-new');
		expect(providerDrafts.list).toHaveLength(0);
	}, 10000);

	const ADD = '/settings/claude-auth/accounts/subscription/add-another';
	const SAVE = 'POST /settings/claude-auth/accounts/subscription/save-current';
	async function openAdd() {
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Add credential to Claude' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Another Subscription/ }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Start sign-in' }));
	}
	/** The worker's cancel found the CLI already signed in, so it kept the sign-in. */
	const keptOnCancel = () => {
		const a = worker.state.claude.accounts!;
		a.subscription = [
			...a.subscription.map((s) => ({ ...s, active: false })),
			subAccount({ id: 'sub-new', kind: 'saved', label: 'late@example.com', email: 'late@example.com', active: true })
		];
		a.default_in_use = false;
		a.login = null;
		a.login_pending = false;
		return { accounts: a, account_id: 'sub-new', login: null };
	};

	it('does not start the sign-in when saving the current one fails for another reason', async () => {
		worker = fakeWorker({ rows: [chatgptRow()], claude: claudeSummary({ accounts: claudeAccounts() }) }, {
			[SAVE]: () => { throw { status: 503, detail: { code: 'storage_unavailable', message: 'Saved sign-ins could not be stored.' } }; }
		});
		await openAdd();
		expect(await screen.findByText('Saved sign-ins could not be stored.')).toBeTruthy();
		expect(worker.sent('POST', ADD)).toHaveLength(0);
		expect(screen.getByRole('button', { name: 'Start sign-in' })).toBeTruthy();
	});

	it('still starts the sign-in when the current one turns out not to be signed in', async () => {
		worker = fakeWorker({ rows: [chatgptRow()], claude: claudeSummary({ accounts: claudeAccounts() }) }, {
			'GET /auth/claude/cli-login': runningTerminal,
			[SAVE]: () => { throw { status: 409, detail: { code: 'not_signed_in', message: 'Claude Code is not signed in.' } }; }
		});
		await openAdd();
		await screen.findByTestId('claude-cli-terminal');
		expect(worker.sent('POST', ADD)).toHaveLength(1);
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('Cancel opens a sign-in the worker kept because it had already finished', async () => {
		worker = fakeWorker({
			rows: [chatgptRow()], subscriptionTerminal: false,
			claude: claudeSummary({ accounts: claudeAccounts({ subscription: [subAccount()] }) })
		}, { 'POST /settings/claude-auth/accounts/subscription/cancel-login': keptOnCancel });
		await openAdd();
		await screen.findByTestId('claude-add-subscription');
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await screen.findByRole('heading', { name: 'late@example.com' });
		expect(settingsNav.selectedItem('providers')).toBe('claude:subscription:sub-new');
		expect(providerDrafts.list).toHaveLength(0);
	});

	it("the terminal's Stop opens a sign-in the worker kept because it had already finished", async () => {
		worker = fakeWorker({
			rows: [chatgptRow()], claude: claudeSummary({ accounts: claudeAccounts({ subscription: [subAccount()] }) })
		}, {
			'GET /auth/claude/cli-login': runningTerminal,
			'POST /settings/claude-auth/accounts/subscription/cancel-login': keptOnCancel
		});
		await openAdd();
		await screen.findByTestId('claude-cli-terminal');
		await fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
		await screen.findByRole('heading', { name: 'late@example.com' });
		expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/cancel-login')).toHaveLength(1);
		expect(worker.sent('POST', '/auth/claude/cli-login/cancel')).toHaveLength(0);
	});

	it('hides Command Prompt when the folder path cannot be quoted for it', async () => {
		worker = fakeWorker({
			rows: [chatgptRow()], subscriptionTerminal: false,
			claude: claudeSummary({ accounts: claudeAccounts({ subscription: [subAccount()] }) })
		}, {
			['POST ' + ADD]: () => {
				const a = worker.state.claude.accounts!;
				a.login = {
					id: 'sub-new', status: 'waiting', error: null, terminal: false, command: 'claude auth login',
					env: { CLAUDE_CONFIG_DIR: '/data/50%/claude-logins/sub-new' },
					commands: {
						powershell: "$env:CLAUDE_CONFIG_DIR='/data/50%/claude-logins/sub-new'; claude auth login",
						cmd: null,
						posix: "CLAUDE_CONFIG_DIR='/data/50%/claude-logins/sub-new' claude auth login"
					}
				};
				a.login_pending = true;
				return { accounts: a, login: a.login };
			}
		});
		await openAdd();
		await screen.findByTestId('claude-add-subscription');
		expect(screen.getByRole('button', { name: 'PowerShell' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Command Prompt' })).toBeNull();
		expect(screen.getByText(/CLAUDE_CONFIG_DIR/)).toBeTruthy();
	});

	it('switches, renames and removes saved sign-ins without changing the Claude Code mode', async () => {
		worker = fakeWorker({
			rows: [chatgptRow()],
			claude: claudeSummary({ accounts: claudeAccounts({
				default_in_use: false,
				subscription: [subAccount({ active: false }), subAccount({ id: 'work', kind: 'saved', label: 'Work', email: 'work@example.com', active: true })]
			}) })
		});
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Use Personal' }));
		await waitFor(() => expect(providers.current.claude?.accounts?.subscription.map((a) => a.active)).toEqual([true, false]));
		expect(worker.sent('POST', '/settings/claude-auth/accounts/subscription/sub-default/activate')).toHaveLength(1);
		expect(group('Claude').getByRole('button', { name: 'Use Work' })).toBeTruthy();
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);

		await fireEvent.click(group('Claude').getByRole('button', { name: 'Work, Saved' }));
		const name = await screen.findByRole('textbox', { name: 'Name' });
		await fireEvent.focus(name); await fireEvent.input(name, { target: { value: 'Team' } }); await fireEvent.blur(name);
		await waitFor(() => expect(worker.sent('PATCH', '/settings/claude-auth/accounts/subscription/work')[0]?.body).toEqual({ label: 'Team' }));
		await screen.findByRole('heading', { name: 'Team' });
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(worker.sent('DELETE', '/settings/claude-auth/accounts/subscription/work')).toHaveLength(1));
		await waitFor(() => expect(providers.current.claude?.accounts?.subscription.map((a) => [a.id, a.active])).toEqual([['sub-default', true]]));
		expect(providers.current.claude?.mode).toBe('subscription');
	});

	it('shows "Sign in again" for a lapsed saved sign-in, with no warning wording', async () => {
		worker = fakeWorker({
			rows: [chatgptRow()],
			claude: claudeSummary({ accounts: claudeAccounts({
				subscription: [subAccount(), subAccount({ id: 'old', kind: 'saved', label: 'Old', active: false, signed_in: false, needs_sign_in: true })]
			}) })
		});
		await show();
		await fireEvent.click(group('Claude').getByRole('button', { name: 'Old, Sign in again' }));
		expect((await screen.findAllByRole('button', { name: /Sign in again/ })).length).toBeGreaterThan(0);
		expect(document.body.textContent).not.toMatch(/unofficial|unsupported/i);
	});
});
