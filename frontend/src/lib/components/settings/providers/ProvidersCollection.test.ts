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
import { resourceSync } from '$lib/services/resource-sync';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { antigravity } from './antigravity.svelte';
import { chatgptRow, claudeSummary, gatewayRow, grokRow, row } from './fixtures';
import { cliStatus, fakeWorker } from './fake-worker';

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
	resourceSync.clear();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

it.each([
	['mp:p1', 'provider:openai'],
	['mp:gw-1', null],
	['mp:ag-1', 'provider:antigravity'],
	['claude:api-key', 'provider:claude']
])('removes %s and returns safely after the detail unmounts', async (item, back) => {
	// Reading the removed row threw on every read and left its page up (#1651).
	const errors: unknown[] = [];
	const onError = (e: ErrorEvent) => errors.push(e.error ?? e.message);
	window.addEventListener('error', onError);
	const consoleError = vi.spyOn(console, 'error').mockImplementation((...args) => errors.push(args));
	const agRow = row({ id: 'ag-1', name: 'Antigravity key', provider_type: 'antigravity', base_url: '',
		auth_mode: 'gemini-api-key' } as Parameters<typeof row>[0]);
	worker = fakeWorker({ rows: [row(), gatewayRow(), agRow] }, {
		'GET /antigravity/status': () => ({ installed: true, version: '1.5.6' }),
		'GET /antigravity/providers/:id/status': () => ({ state: 'signed_in', login: null }),
		'DELETE /model-providers/:id': (_body, path) => {
			worker.state.rows = worker.state.rows.filter((r) => r.id !== path.split('/')[2]);
			return { success: true };
		},
		'DELETE /settings/integrations/anthropic': () => {
			worker.state.integrations.anthropic_api_key_set = false;
			worker.state.integrations.anthropic_api_key_masked = '';
			worker.state.claude = claudeSummary({ ...worker.state.claude, api_key: { set: false, last4: null } });
			return { success: true };
		}
	});
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.stacked = true;
	settingsNav.go({ category: 'providers', item });
	render(ProvidersCollection);
	await flush();
	await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
	await waitFor(() => expect(settingsNav.selectedItem('providers')).toBe(back));
	if (!back) expect(settingsNav.view).toBe('page');
	else expect(screen.queryByText('Select a provider or credential.')).toBeNull();
	await flush();
	window.removeEventListener('error', onError);
	consoleError.mockRestore();
	expect(errors.filter((e) => /reading '(credential|provider)'/.test(String(e)))).toEqual([]);
});

describe('the list', () => {
	it.each([
		['page open', 1], ['page open', 3], ['Retry', 1], ['Retry', 3]
	] as const)('replays %s after %i real settings invalidations without losing a credential deletion', async (source, events) => {
		vi.useFakeTimers();
		worker = fakeWorker({ rows: [row()] }, {
			'DELETE /model-providers/:id': (_body, path) => {
				worker.state.rows = worker.state.rows.filter((r) => r.id !== path.split('/')[2]);
				return { success: true };
			}
		});
		const heldCycle = source === 'Retry' ? 2 : 1;
		let registryReads = 0;
		let claudeReads = 0;
		let releaseCurrent!: () => void;
		let releaseTrailing!: () => void;
		const current = new Promise<void>((resolve) => (releaseCurrent = resolve));
		const trailing = new Promise<void>((resolve) => (releaseTrailing = resolve));
		const real = worker.fetch;
		vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
			const path = new URL(String(input), 'http://localhost').pathname;
			if ((init?.method ?? 'GET') === 'GET' && path === '/api/v1/model-providers') {
				registryReads++;
				const response = await real(input, init); // snapshot this request's worker data
				if (registryReads === heldCycle + 1) await trailing;
				return response;
			}
			if (path === '/api/v1/settings/claude-auth') {
				claudeReads++;
				if (source === 'Retry' && claudeReads === 1) return new Response('unavailable', { status: 503 });
				if (claudeReads === heldCycle) await current;
				if (claudeReads === heldCycle + 1) await trailing;
			}
			return real(input, init);
		});
		render(ProvidersCollection);
		await vi.advanceTimersByTimeAsync(0);
		if (source === 'Retry') {
			await fireEvent.click(screen.getByRole('button', { name: 'Retry providers' }));
			await vi.advanceTimersByTimeAsync(0);
		}
		expect(providers.current.providers.map((p) => p.id)).toEqual(['p1']);
		expect(registryReads).toBe(heldCycle);
		expect(providers.current.refreshing).toBe(true);
		// Another tab changes worker data, then the actual mounted resource watcher runs.
		expect((await fetch('/api/v1/model-providers/p1', { method: 'DELETE' })).ok).toBe(true);
		for (let i = 0; i < events; i++) {
			resourceSync.invalidate(['settings']);
			await vi.advanceTimersByTimeAsync(100);
		}
		expect(registryReads).toBe(heldCycle); // never run overlapping full snapshots
		releaseCurrent();
		await vi.advanceTimersByTimeAsync(0);
		expect(registryReads).toBe(heldCycle + 1); // exactly one trailing read for this burst
		expect(providers.current.refreshing).toBe(true); // includes the trailing read
		if (source === 'page open') expect(providers.current.initialSnapshotReady).toBe(false);
		releaseTrailing();
		await vi.advanceTimersByTimeAsync(1000);
		expect(registryReads).toBe(heldCycle + 1); // ResourceSync must not schedule a duplicate
		expect(providers.current.refreshing).toBe(false);
		expect(providers.current.providers).toEqual([]);
		expect(screen.queryByRole('group', { name: 'OpenAI' })).toBeNull();
		expect(screen.getByText('Not connected')).toBeTruthy();
		expect(screen.getByRole('button', { name: /^OpenAI / }).getAttribute('data-settings-item')).toBe('provider:openai');
	});

	it('keeps a timed-out registry unknown while its retry response is held', async () => {
		vi.useFakeTimers();
		let registryReads = 0;
		let releaseRetry!: () => void;
		const heldRetry = new Promise<void>((resolve) => (releaseRetry = resolve));
		const real = worker.fetch;
		vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
			if (new URL(String(input), 'http://localhost').pathname === '/api/v1/model-providers') {
				registryReads++;
				if (registryReads === 1) return new Promise<Response>(() => {});
				if (registryReads === 2) await heldRetry;
			}
			return real(input, init);
		});
		render(ProvidersCollection);
		await vi.advanceTimersByTimeAsync(10_000);
		expect(providers.current.providersError).toContain('did not respond');
		await fireEvent.click(screen.getByRole('button', { name: 'Retry providers' }));
		await vi.advanceTimersByTimeAsync(0);
		expect(registryReads).toBe(2);
		expect(providers.current.providersError).toContain('did not respond');
		expect(screen.queryByText('Not connected')).toBeNull();
		expect(screen.queryByRole('button', { name: /^OpenAI/ })).toBeNull();
		settingsNav.go({ category: 'providers', item: 'provider:openai' });
		await vi.advanceTimersByTimeAsync(9_000);
		expect(settingsNav.selectedItem('providers')).toBe('provider:openai');
		expect(screen.queryByRole('heading', { level: 2, name: 'OpenAI' })).toBeNull();
		expect(screen.queryByText(/Not connected/)).toBeNull();
		expect(screen.getAllByRole('button', { name: 'Retry providers', hidden: true }).every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
		releaseRetry();
		await vi.advanceTimersByTimeAsync(0);
		expect(providers.current.providersError).toBe('');
		expect(screen.getByRole('heading', { level: 2, name: 'OpenAI' })).toBeTruthy();
		expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, Active', 'OpenAI API key, Active']);
	});

	it.each(['second Retry', 'another caller'])(
		'coalesces a held retry with a %s so an older deadline cannot undo recovery',
		async (source) => {
			vi.useFakeTimers();
			let integrationReads = 0;
			let releaseOld!: () => void;
			const heldOld = new Promise<void>((resolve) => (releaseOld = resolve));
			const real = worker.fetch;
			vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
				if (new URL(String(input), 'http://localhost').pathname === '/api/v1/settings/integrations') {
					integrationReads++;
					if (integrationReads === 1) return new Response('unavailable', { status: 503 });
					if (integrationReads === 2) {
						await heldOld;
						return new Response('obsolete error', { status: 503 });
					}
				}
				return real(input, init);
			});
			settingsNav.stacked = true;
			settingsNav.go({ category: 'providers', item: 'fal:key' });
			render(ProvidersCollection);
			await vi.advanceTimersByTimeAsync(0);
			const retries = screen.getAllByRole('button', { name: 'Retry providers', hidden: true }) as HTMLButtonElement[];
			expect(retries).toHaveLength(2);
			await fireEvent.click(retries[1]);
			await vi.advanceTimersByTimeAsync(0);
			const first = providers.refreshAll();
			const second = source === 'second Retry'
				? fireEvent.click(retries[0])
				: providers.refreshAll();
			await vi.advanceTimersByTimeAsync(0);
			expect(integrationReads).toBe(2); // no successful request may overtake the held retry
			expect(retries.every((b) => b.disabled)).toBe(true);
			expect(providers.current.refreshing).toBe(true);
			if (source === 'another caller') expect(second).toBe(first);
			await vi.advanceTimersByTimeAsync(10_000);
			await first;
			expect(providers.current.refreshing).toBe(false);
			expect(retries.every((b) => !b.disabled)).toBe(true);
			expect(providers.current.integrationsError).toContain('did not respond');
			await fireEvent.click(retries[1]);
			await vi.advanceTimersByTimeAsync(0);
			expect(integrationReads).toBe(3);
			expect(screen.queryByRole('alert')).toBeNull();
			expect(screen.getByRole('heading', { level: 2, name: 'Media key' })).toBeTruthy();
			releaseOld();
			await vi.advanceTimersByTimeAsync(10_000);
			expect(providers.current.integrationsError).toBeNull();
			expect(screen.queryByRole('alert')).toBeNull();
			expect(settingsNav.selectedItem('providers')).toBe('fal:key');
		}
	);

	it.each(['/settings/claude-auth', '/settings/integrations', '/model-providers'])(
		'shows loaded providers and a working retry when %s never responds',
		async (path) => {
			vi.useFakeTimers();
			let stalled = true;
			const real = worker.fetch;
			vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
				if (stalled && new URL(String(input), 'http://localhost').pathname === `/api/v1${path}`) {
					return new Promise<Response>(() => {});
				}
				return real(input, init);
			});
			if (path === '/settings/integrations') {
				settingsNav.stacked = true;
				settingsNav.go({ category: 'providers', item: 'fal:key' });
			}
			const { container } = render(ProvidersCollection);
			await vi.advanceTimersByTimeAsync(0);
			expect(providers.current.initialSnapshotReady).toBe(false);
			await vi.advanceTimersByTimeAsync(10_000);
			expect(providers.current.initialSnapshotReady).toBe(true);
			expect(container.querySelector('.pl')).not.toBeNull();
			expect(screen.getByRole('alert').textContent).toContain('did not respond');
			expect(screen.queryByText('Not connected')).toBeNull();
			const retrySurface = path === '/settings/integrations'
				? within(container.querySelector('.st-empty-detail') as HTMLElement)
				: screen;
			if (path === '/settings/integrations') expect(settingsNav.selectedItem('providers')).toBe('fal:key');
			stalled = false;
			await fireEvent.click(retrySurface.getByRole('button', { name: 'Retry providers' }));
			await vi.advanceTimersByTimeAsync(0);
			expect(screen.queryByRole('alert')).toBeNull();
			expect(credentialNames('fal.ai')).toEqual(['Media key, On']);
			if (path === '/settings/integrations') {
				expect(settingsNav.selectedItem('providers')).toBe('fal:key');
				expect(screen.getByRole('heading', { level: 2, name: 'Media key' })).toBeTruthy();
			}
		}
	);

	it.each(['fal:key', 'provider:fal'])(
		'keeps the cold %s deep link selected without classifying partial details',
		async (item) => {
			let release!: () => void;
			const held = new Promise<void>((r) => (release = r));
			const real = worker.fetch;
			vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
				if (new URL(String(input), 'http://localhost').pathname === '/api/v1/settings/integrations') await held;
				return real(input, init);
			});
			settingsNav.stacked = true;
			settingsNav.go({ category: 'providers', item });
			render(ProvidersCollection);
			await flush();
			expect(settingsNav.selectedItem('providers')).toBe(item);
			expect(screen.getByText('Loading providers', { exact: true })).toBeTruthy();
			expect(screen.queryByText(/Not connected/)).toBeNull();
			release();
			await flush();
			expect(settingsNav.selectedItem('providers')).toBe(item);
			expect(screen.queryByText('Loading providers', { exact: true })).toBeNull();
			expect(screen.getByRole('heading', { level: 2, name: item === 'fal:key' ? 'Media key' : 'fal.ai' })).toBeTruthy();
		}
	);

	it.each(['/settings/claude-auth', '/settings/integrations', '/model-providers'])(
		'waits for the complete first snapshot when %s is slow, rather than moving provisional groups',
		async (path) => {
			let release!: () => void;
			const held = new Promise<void>((r) => (release = r));
			const real = worker.fetch;
			vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
				if (new URL(String(input), 'http://localhost').pathname === `/api/v1${path}`) await held;
				return real(input, init);
			});
			const { container } = render(ProvidersCollection);
			await flush();
			expect(screen.getByRole('status').textContent).toBe('Loading providers…');
			const idle = container.querySelector('.st-dpane > .st-empty-detail[data-idle]');
			expect(idle).not.toBeNull(); // keeps the unselected desktop list full-width
			expect(container.querySelector('.pl')).toBeNull();
			expect(screen.queryByText('Not connected')).toBeNull();
			release();
			await flush();
			expect(screen.queryByText('Loading providers…')).toBeNull();
			expect(container.querySelector('.st-dpane > .st-empty-detail[data-idle]')).toBe(idle);
			expect(credentialNames('Claude')[0]).toBe('Claude Max subscription, Active for Claude');
			expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, Active', 'OpenAI API key, Active']);
			expect(screen.getAllByText('Claude')).toHaveLength(1);
			expect(screen.getByText('Not connected')).toBeTruthy();
		}
	);

	it('keeps the complete list visible during a later silent refresh', async () => {
		await show();
		const before = group('Claude');
		let release!: () => void;
		const held = new Promise<void>((r) => (release = r));
		const real = worker.fetch;
		vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
			await held;
			return real(input, init);
		});
		const refresh = providers.refreshAll();
		await flush();
		expect(group('Claude')).toBe(before);
		expect(screen.queryByText('Loading providers…')).toBeNull();
		release();
		await refresh;
	});

	it('keeps the Add provider name when the narrow list hides its label', async () => {
		await show();
		const add = screen.getByRole('button', { name: 'Add provider' });
		expect(add.getAttribute('title')).toBe('Add provider');
	});

	it('groups connected credentials under their provider and lists the rest as Not connected', async () => {
		await show();
		expect(screen.getByText('5 connected')).toBeTruthy();
		expect(credentialNames('Claude')).toEqual([
			'Claude Max subscription, Active for Claude',
			'Anthropic API key, Active for Claude API'
		]);
		expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, Active', 'OpenAI API key, Active']);
		expect(within(group('OpenAI')).getByText('owner@example.com, Plus plan')).toBeTruthy();
		expect(credentialNames('xAI Grok')).toEqual(['SuperGrok subscription, Reconnect needed']);
		expect(credentialNames('fal.ai')).toEqual(['Media key, On']);
		expect(credentialNames('ServerWorker vLLM')).toEqual(['Gateway key, On']);

		expect(screen.getByText('Not connected')).toBeTruthy();
		const openRouter = screen.getByRole('button', { name: /^OpenRouter/ });
		expect(openRouter.getAttribute('data-settings-item')).toBe('provider:openrouter');
		expect(screen.queryByRole('group', { name: 'OpenRouter' })).toBeNull();
	});

	it('has a switch on every credential, Claude’s included', async () => {
		await show();
		// The subscription has a switch; this fixture's key predates saved keys
		// (no accounts list), so it has none, as before.
		expect(within(group('Claude')).getByRole('switch', { name: /subscription/ })).toBeTruthy();
		expect(within(group('OpenAI')).getAllByRole('switch')).toHaveLength(2);
		expect(within(group('fal.ai')).getByRole('switch', { name: 'Media key on' })).toBeTruthy();
	});

	it('opens a credential’s detail from its row', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'OpenAI API key, Active' }));
		expect(settingsNav.selectedItem('providers')).toBe('mp:p1');
		expect(await screen.findByRole('tab', { name: 'Overview' })).toBeTruthy();
		expect(screen.getByRole('tab', { name: 'Models' })).toBeTruthy();
		expect(screen.queryByRole('tab', { name: 'Usage' })).toBeNull();
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
		await waitFor(() => expect(worker.sent('POST', '/model-providers/p1/activate')).toHaveLength(1));
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
	it("gives Claude's rows the same shape as the others: one open button and a switch", async () => {
		await show();
		const row = group('Claude').querySelector('.cred') as HTMLElement;
		expect(within(row).getAllByRole('button')).toHaveLength(1);
		expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('true');
		expect(row.querySelector('.sm-kind')?.textContent).toContain('Subscription');
	});

	it('turns the subscription off with its switch and leaves the API key on', async () => {
		await show();
		const before = credentialNames('Claude');
		await fireEvent.click(within(group('Claude')).getByRole('switch', { name: /subscription/ }));
		await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(1));
		expect(worker.sent('PUT', '/settings/claude-auth')[0].body).toEqual({ mode: 'off' });
		await waitFor(() => expect(credentialNames('Claude')[0]).toBe('Claude Max subscription, Off'));
		// The key row is untouched: its own switch, still on.
		expect(credentialNames('Claude')[1]).toBe(before[1]);
		expect(credentialNames('Claude')[1]).not.toMatch(/, Off$/);
		expect(worker.sent('DELETE', '/settings/integrations/anthropic')).toHaveLength(0);
	});

	it("has no mode picker on Claude's page", async () => {
		await show();
		await fireEvent.click(within(group('Claude')).getByRole('button', { name: 'Claude' }));
		await flush();
		expect(screen.queryByRole('group', { name: 'Runs on' })).toBeNull();
	});
});

describe('adding', () => {
	it('"+" offers the kinds not added yet and makes one draft row', async () => {
		await show();
		const plus = within(group('OpenAI')).getByRole('button', {
			name: 'Add account to OpenAI'
		});
		await fireEvent.click(plus);
		const menu = await screen.findByRole('menu', { name: 'Add to OpenAI' });
		expect(
			within(menu)
				.getAllByRole('menuitem')
				.map((m) => m.firstElementChild?.textContent)
		).toEqual(['Another Subscription', 'Another API key']);
		await fireEvent.click(within(menu).getByRole('menuitem', { name: /Another API key/ }));
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
			expect(credentialNames('OpenAI')).toEqual(['OpenAI Plus subscription, Active', 'OpenAI API key, Active', 'OpenAI API key 2, Setting up'])
		);
		expect(screen.getByLabelText('API key')).toBeTruthy();
		expect(worker.sent('POST', '/model-providers')).toHaveLength(0);
	});

	it('"+" offers another subscription when one is already added', async () => {
		start({ rows: [chatgptRow()] });
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add account to OpenAI' }));
		const menu = await screen.findByRole('menu');
		expect(
			within(menu)
				.getAllByRole('menuitem')
				.map((m) => m.firstElementChild?.textContent)
		).toEqual(['Another Subscription', 'API key']);
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
	it('disables the switches (Claude’s too) and Add provider, and hides "+"', async () => {
		await show({ readOnly: true });
		expect(within(group('Claude')).getAllByRole('switch').length).toBeGreaterThan(0);
		for (const sw of screen.getAllByRole('switch')) expect((sw as HTMLButtonElement).disabled).toBe(true);
		expect(screen.queryByRole('button', { name: 'Add account to OpenAI' })).toBeNull();
		expect(
			(
				screen.getByRole('button', {
					name: 'Add provider'
				}) as HTMLButtonElement
			).disabled
		).toBe(true);
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

describe('Claude Code on the Not connected page', () => {
	function notSignedIn() {
		const claude = claudeSummary({ api_key: { set: false, last4: null } });
		claude.cli = { ...claude.cli, signed_in: false, email: null, plan: null };
		return claude;
	}

	it('offers Set up with no Claude account row, in every mode, and no mode control', async () => {
		for (const mode of ['subscription', 'off'] as const) {
			cleanup();
			providers.clear();
			settingsNav.reset();
			worker = fakeWorker({ claude: { ...notSignedIn(), mode }, cli: cliStatus({ signed_in: false }) });
			worker.state.integrations = { ...worker.state.integrations, anthropic_api_key_set: false };
			vi.stubGlobal('fetch', worker.fetch);
			await providers.refreshAll();
			settingsNav.go({ category: 'providers', item: 'provider:claude' });
			render(ProvidersCollection);
			await flush();
			expect(screen.queryByRole('group', { name: 'Runs on' })).toBeNull();
			expect(within(screen.getByRole('region', { name: 'Connect' })).getAllByRole('button', { name: 'Set up' }).length).toBeGreaterThan(0);
		}
	});

	it('keeps the removed confirmation on screen until it is dismissed', async () => {
		const removed = 'Claude Code removed. Your sign-in and settings were kept.';
		worker = fakeWorker({ claude: notSignedIn(), cli: cliStatus({ signed_in: false }) }, {
			'POST /settings/claude-cli/remove': () => {
				worker.state.claude = { ...worker.state.claude, cli: { ...worker.state.claude.cli, installed: false } };
				worker.state.cli = { ...worker.state.cli, installed: false };
				return { installed: false, version: null, path: null, check_updates: true, latest_version: null, installed_by_app: false,
					can_install: true, can_remove: false, platform: 'docker',
					job: { kind: 'remove', state: 'succeeded', started_at: '2026-10-06T00:00:00Z',
						finished_at: '2026-10-06T00:00:05Z', message: removed, output_tail: '' } };
			}
		});
		worker.state.integrations = { ...worker.state.integrations, anthropic_api_key_set: false };
		vi.stubGlobal('fetch', worker.fetch);
		await providers.refreshAll();
		settingsNav.go({ category: 'providers', item: 'provider:claude' });
		render(ProvidersCollection);
		await flush();
		// The Not connected page: Set up rows, no Credentials group.
		expect(screen.getAllByRole('button', { name: 'Set up' }).length).toBeGreaterThan(0);
		expect(screen.queryByRole('region', { name: /^Credentials/ })).toBeNull();

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }));
		await flush(10);

		expect(worker.sent('POST', '/settings/claude-cli/remove')).toHaveLength(1);
		expect(providers.current.claude?.cli?.installed).toBe(false);
		expect(await screen.findByText(removed)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Install Claude Code' })).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		await flush();
		expect(screen.queryByText(removed)).toBeNull();
		expect(screen.queryByTestId('claude-cli-install')).toBeNull();
	});
});

describe('Antigravity on the Not connected page', () => {
	const meta = { auth_modes: [], dashboard_url: '', guard_default: true, default_models: [] };

	async function open(status: Record<string, unknown>, routes: Parameters<typeof fakeWorker>[1] = {}) {
		antigravity.status = null; antigravity.meta = null; antigravity.error = null;
		worker = fakeWorker({ rows: [row()] }, {
			'GET /antigravity/meta': () => meta,
			'GET /antigravity/status': () => status,
			...routes
		});
		vi.stubGlobal('fetch', worker.fetch);
		await providers.refreshAll();
		settingsNav.go({ category: 'providers', item: 'provider:antigravity' });
		render(ProvidersCollection);
		await flush(10);
	}

	it('shows the installed agent and saves its update check with nothing connected', async () => {
		await open({ installed: true, version: '1.5.6', source: 'app', can_install: true, check_updates: true,
			latest_version: '1.5.13', install: null, workers_requiring_update: [] }, {
			'PUT /antigravity/update-check': (body) => ({ check_updates: (body as { check_updates: boolean }).check_updates,
				latest_version: null })
		});
		expect(screen.getAllByRole('button', { name: 'Set up' }).length).toBeGreaterThan(0);
		expect(screen.getByText('Version 1.5.13 is available')).toBeTruthy();
		const check = screen.getByRole('switch', { name: 'Check for updates at startup' });
		expect(check.getAttribute('aria-checked')).toBe('true');

		await fireEvent.click(check);
		await flush();

		expect(worker.sent('PUT', '/antigravity/update-check').map((c) => c.body)).toEqual([{ check_updates: false }]);
		expect(antigravity.status?.check_updates).toBe(false);
		// One read from the page and one from the section it mounts: no reload loop.
		expect(worker.sent('GET', '/antigravity/status').length).toBeLessThanOrEqual(2);
	});

	it('shows no agent section while Antigravity is not installed', async () => {
		await open({ installed: false, version: null, source: null, can_install: true, check_updates: true,
			latest_version: null, install: null, workers_requiring_update: [] });
		expect(screen.getAllByRole('button', { name: 'Set up' }).length).toBeGreaterThan(0);
		expect(screen.queryByRole('switch', { name: 'Check for updates at startup' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Install Antigravity' })).toBeNull();
		expect(worker.sent('GET', '/antigravity/status')).toHaveLength(1);
	});
});
