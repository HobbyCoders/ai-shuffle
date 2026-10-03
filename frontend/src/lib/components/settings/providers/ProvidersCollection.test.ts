/**
 * Settings > Providers: the list (grouping, Not connected, the connector rows
 * and their switches), the "+" menu and "Add provider" drafts, Claude's mode
 * control, read-only access, and registry coverage. The store is real; the
 * worker is faked at `fetch` so the exact requests can be asserted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { chatgptRow, gatewayRow, grokRow, row } from './fixtures';
import { fakeWorker } from './fake-worker';

let worker: ReturnType<typeof fakeWorker>;

function start(init: Parameters<typeof fakeWorker>[0] = {}) {
	worker = fakeWorker({
		rows: [chatgptRow({ account_email: 'owner@example.com', account_plan: 'plus' }), row(), grokRow(), gatewayRow()],
		...init
	});
	vi.stubGlobal('fetch', worker.fetch);
}

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

/** Render with the store already loaded, as after the first open. */
async function show(options: { readOnly?: boolean } = {}) {
	await providers.refreshAll();
	const context = options.readOnly ? new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) : undefined;
	const result = render(ProvidersCollection, context ? { context } : {});
	await flush();
	return result;
}

const group = (name: string) => screen.getByRole('group', { name });
const credentialNames = (name: string) =>
	within(group(name))
		.queryAllByRole('button')
		.map((b) => b.getAttribute('aria-label'))
		.filter((label): label is string => !!label && label.includes(', '));

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
	start();
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('the list', () => {
	it('groups connected credentials under their provider and lists the rest as Not connected', async () => {
		await show();
		expect(screen.getByText('5 connected')).toBeTruthy();
		expect(credentialNames('Claude')).toEqual([
			'Claude Max subscription, Active for Claude',
			'Anthropic API key, Standby'
		]);
		expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, On', 'OpenAI API key, On']);
		expect(within(group('OpenAI')).getByText('owner@example.com, Plus plan')).toBeTruthy();
		expect(credentialNames('xAI Grok')).toEqual(['SuperGrok subscription, Reconnect needed']);
		expect(credentialNames('fal.ai')).toEqual(['Media key, On']);
		expect(credentialNames('Homelab vLLM')).toEqual(['Gateway key, On']);
		expect(within(group('OpenAI')).getByText('2 on')).toBeTruthy();

		expect(screen.getByText('Not connected')).toBeTruthy();
		const openRouter = screen.getByRole('button', { name: /^OpenRouter/ });
		expect(openRouter.getAttribute('data-settings-item')).toBe('provider:openrouter');
		expect(screen.queryByRole('group', { name: 'OpenRouter' })).toBeNull();
	});

	it('has a switch on every credential but Claude’s', async () => {
		await show();
		expect(within(group('Claude')).queryAllByRole('switch')).toHaveLength(0);
		expect(within(group('OpenAI')).getAllByRole('switch')).toHaveLength(2);
		expect(within(group('fal.ai')).getByRole('switch', { name: 'Media key on' })).toBeTruthy();
	});

	it('opens a credential’s detail from its row', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'OpenAI API key, On' }));
		expect(settingsNav.selectedItem('providers')).toBe('mp:p1');
		expect(await screen.findByRole('tab', { name: /Usage/ })).toBeTruthy();
	});
});

describe('switches', () => {
	it('turns a model provider row off with PATCH {enabled}, never /toggle', async () => {
		await show();
		const sw = within(group('OpenAI')).getByRole('switch', {
			name: 'OpenAI API key on'
		});
		await fireEvent.click(sw);
		await waitFor(() => expect(worker.sent('PATCH', '/model-providers/p1')).toHaveLength(1));
		expect(worker.sent('PATCH', '/model-providers/p1')[0].body).toEqual({
			enabled: false
		});
		await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('false'));
		await fireEvent.click(sw);
		await waitFor(() => expect(worker.sent('PATCH', '/model-providers/p1')).toHaveLength(2));
		expect(worker.sent('PATCH', '/model-providers/p1')[1].body).toEqual({
			enabled: true
		});
		expect(worker.calls.some((c) => c.path.includes('/toggle'))).toBe(false);
	});

	it('rolls the switch back when the save fails', async () => {
		worker = fakeWorker(
			{ rows: [row()] },
			{
				'PATCH /model-providers/:id': () => {
					throw { status: 500, detail: 'Worker busy' };
				}
			}
		);
		vi.stubGlobal('fetch', worker.fetch);
		await show();
		const sw = screen.getByRole('switch', { name: 'OpenAI API key on' });
		await fireEvent.click(sw);
		await waitFor(() => expect(worker.sent('PATCH', '/model-providers/p1')).toHaveLength(1));
		await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('true'));
	});

	it('turns fal.ai off with its own flag and keeps the key', async () => {
		await show();
		await fireEvent.click(screen.getByRole('switch', { name: 'Media key on' }));
		await waitFor(() => expect(worker.sent('PUT', '/settings/integrations/fal-ai/enabled')).toHaveLength(1));
		expect(worker.sent('PUT', '/settings/integrations/fal-ai/enabled')[0].body).toEqual({ enabled: false });
		expect(worker.sent('DELETE', '/settings/integrations/fal-ai')).toHaveLength(0);
		await waitFor(() => expect(screen.getByRole('button', { name: 'Media key, Off' })).toBeTruthy());
	});
});

// Ported from main's MediaToolsSection test (8fc5f1b): the fal.ai balance is
// read with the ordinary key, so there is no separate Admin billing key.
describe('fal.ai', () => {
	it('offers only the ordinary key, with no separate Admin billing key', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Media key, On' }));
		await flush();
		const detail = screen.getByRole('heading', { level: 2, name: 'Media key' }).closest('.st-dwrap') as HTMLElement;
		expect(detail).toBeTruthy();
		expect(detail.textContent).not.toMatch(/Admin|billing key/i);
		expect(detail.querySelector('[aria-label="fal.ai billing access"]')).toBeNull();
		// One key field: replacing the ordinary media key.
		const secrets = [...detail.querySelectorAll('input[type="password"]')];
		expect(secrets.map((i) => i.getAttribute('aria-label'))).toEqual(['New fal.ai key']);
		expect(worker.calls.some((c) => /billing/i.test(c.path))).toBe(false);
	});
});

describe('Claude', () => {
	it('turns Claude off from the header', async () => {
		await show();
		const modes = within(group('Claude')).getByRole('group', {
			name: 'Claude runs on'
		});
		expect(within(modes).getByRole('button', { name: 'Subscription' }).getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(within(modes).getByRole('button', { name: 'Off' }));
		await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(1));
		expect(worker.sent('PUT', '/settings/claude-auth')[0].body).toEqual({
			mode: 'off'
		});
		await waitFor(() =>
			expect(within(modes).getByRole('button', { name: 'Off' }).getAttribute('aria-pressed')).toBe('true')
		);
		expect(credentialNames('Claude')).toEqual(['Claude Max subscription, Off', 'Anthropic API key, Off']);
	});

	it('does not save when the current mode is picked again', async () => {
		await show();
		const modes = within(group('Claude')).getByRole('group', {
			name: 'Claude runs on'
		});
		await fireEvent.click(within(modes).getByRole('button', { name: 'Subscription' }));
		await flush();
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	});
});

describe('adding', () => {
	it('"+" offers the kinds not added yet and makes one draft row', async () => {
		await show();
		const plus = within(group('OpenAI')).getByRole('button', {
			name: 'Add credential to OpenAI'
		});
		await fireEvent.click(plus);
		const menu = await screen.findByRole('menu', { name: 'Add to OpenAI' });
		expect(
			within(menu)
				.getAllByRole('menuitem')
				.map((m) => m.firstElementChild?.textContent)
		).toEqual(['Another API key']);
		await fireEvent.click(within(menu).getByRole('menuitem'));
		expect(screen.queryByRole('menu')).toBeNull();

		expect(providerDrafts.list).toHaveLength(1);
		const draft = providerDrafts.list[0];
		expect(draft).toMatchObject({
			providerId: 'openai',
			flavor: 'key',
			name: 'OpenAI API key 2'
		});
		expect(settingsNav.selectedItem('providers')).toBe(draft.item);
		await waitFor(() =>
			expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, On', 'OpenAI API key, On', 'OpenAI API key 2, Setting up'])
		);
		expect(screen.getByLabelText('API key')).toBeTruthy();
		expect(worker.sent('POST', '/model-providers')).toHaveLength(0);
	});

	it('"+" leaves out a subscription that is already added', async () => {
		start({ rows: [chatgptRow()] });
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add credential to OpenAI' }));
		const menu = await screen.findByRole('menu');
		expect(
			within(menu)
				.getAllByRole('menuitem')
				.map((m) => m.firstElementChild?.textContent)
		).toEqual(['API key']);
	});

	it('"Add provider" makes a new gateway draft', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));
		expect(providerDrafts.gateways).toHaveLength(1);
		await waitFor(() => expect(credentialNames('New provider')).toEqual(['Gateway key, Setting up']));
		expect(within(group('New provider')).getAllByText('Setting up')).toHaveLength(2);
		expect(screen.getByLabelText('Name')).toBeTruthy();
		expect(screen.getByLabelText('Endpoint')).toBeTruthy();
		expect(settingsNav.selectedItem('providers')).toBe(providerDrafts.list[0].item);
	});
});

describe('read-only', () => {
	it('disables the switches, the modes, "+" and Add provider', async () => {
		await show({ readOnly: true });
		for (const sw of screen.getAllByRole('switch')) expect((sw as HTMLButtonElement).disabled).toBe(true);
		const modes = within(group('Claude')).getByRole('group', {
			name: 'Claude runs on'
		});
		for (const b of within(modes).getAllByRole('button')) expect((b as HTMLButtonElement).disabled).toBe(true);
		expect(
			(
				screen.getByRole('button', {
					name: 'Add credential to OpenAI'
				}) as HTMLButtonElement
			).disabled
		).toBe(true);
		expect(
			(
				screen.getByRole('button', {
					name: 'Add provider'
				}) as HTMLButtonElement
			).disabled
		).toBe(true);
		await fireEvent.click(within(modes).getByRole('button', { name: 'Off' }));
		expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	});
});

describe('coverage', () => {
	it('renders every Providers registry entry where the registry says it lives', async () => {
		start({ rows: [row()] });
		await providers.refreshAll();
		expect(worker.state.loopGuard.enabled).toBe(true);
		expect(worker.state.cli.signed_in).toBe(true);
		await assertCategoryCoverage(
			'providers',
			({ item, tab }) => {
				// Each place opens fresh, so an item's remembered tab does not carry over.
				settingsNav.reset();
				settingsNav.go({ category: 'providers', item, tab });
				return render(ProvidersCollection);
			},
			{ settle: () => flush(8) }
		);
	});
});
