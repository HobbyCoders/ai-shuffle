/**
 * Model curation, in a credential's Models tab and right after a key is
 * added. Ported from ProviderPanel.reasoning-metadata and ProviderAddFlows:
 * saved entries keep context_window, pricing and reasoning metadata.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { ModelProvider, ProviderModelEntry } from '$lib/api/model-providers';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	post: vi.fn(),
	patch: vi.fn(),
	refresh: vi.fn(),
	notify: vi.fn(),
	addAnthropicAccount: vi.fn(),
	loadClaude: vi.fn(),
	installs: [] as Array<Record<string, unknown>>
}));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post, patch: mocks.patch },
	getErrorMessage: (e: { message?: string } | undefined, fallback: string) => e?.message ?? fallback
}));
vi.mock('$lib/stores/providers.svelte', async () => {
	const api = await import('$lib/api/model-providers');
	// As the store: one running fetch per provider, joined by a remount.
	const running = new Map<string, Promise<unknown>>();
	return {
		providers: {
			current: { providers: [], claude: null },
			fetchProviderModels: (id: string) => {
				if (!running.has(id)) {
					running.set(id, api.fetchProviderModels(id).finally(() => running.delete(id)));
				}
				return running.get(id);
			},
			modelFetchRunning: (id: string) => running.has(id),
			saveProvider: (data: Parameters<typeof api.updateModelProvider>[1], id: string) =>
				api.updateModelProvider(id, data),
			refreshProviders: mocks.refresh,
			addAnthropicAccount: mocks.addAnthropicAccount,
			loadClaude: mocks.loadClaude
		}
	};
});
vi.mock('$lib/stores/notifications', () => ({ notify: mocks.notify }));
vi.mock('$lib/stores/worker-presence', async () => {
	const { writable } = await import('svelte/store');
	const installs = writable<Array<Record<string, unknown>> | null>(null);
	return {
		workerInstalls: { subscribe: installs.subscribe },
		refreshWorkerPresence: async () => { installs.set(mocks.installs); return mocks.installs; }
	};
});

import { resourceSync } from '$lib/services/resource-sync';
import ModelCuration from './ModelCuration.svelte';
import DraftHarness from './DraftHarness.test.svelte';
import { providerDrafts } from './drafts.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { row } from './fixtures';

const raw = {
	supported_parameters: [],
	supported_reasoning_levels: [],
	default_reasoning_level: null,
	reasoning_capabilities: { version: 1 as const, effort_levels: [], default_effort: null, mandatory: true }
};
const stale = {
	supported_parameters: ['reasoning'],
	supported_reasoning_levels: ['high'],
	default_reasoning_level: 'high'
};
const entry = (id: string, metadata: Partial<ProviderModelEntry> = raw): ProviderModelEntry => ({
	id,
	label: id,
	...metadata
});
const router = (models: ProviderModelEntry[]): ModelProvider =>
	row({
		id: 'router',
		name: 'OpenRouter API key',
		provider_type: 'openrouter',
		base_url: 'https://openrouter.ai/api',
		catalog_binding: 'fixture-binding-a',
		models
	});
const saved = () => mocks.patch.mock.calls.at(-1)![1].models;
const toggle = async (id: string) => {
	await fireEvent.click(await screen.findByRole('checkbox', { name: id }));
	await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
};

beforeEach(() => {
	vi.resetAllMocks();
	mocks.get.mockResolvedValue(null);
	mocks.patch.mockResolvedValue({});
	mocks.refresh.mockResolvedValue(undefined);
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
});
afterEach(cleanup);

it('names the checkbox after the visible model and announces the provider separately', async () => {
	const model = entry('model-a', { label: 'Model A' });
	render(ModelCuration, { row: router([model]) });
	const checkbox = await screen.findByRole('checkbox', { name: 'model-a Model A', description: 'OpenRouter API key' });
	expect((checkbox as HTMLInputElement).checked).toBe(true);
});

it('can re-enable the fixed Kimi Code model after disabling it and reopening Manage models', async () => {
	const model = entry('kimi-for-coding');
	const preset = { source: 'preset', binding: 'kimi-binding', models: [model] };
	mocks.post.mockResolvedValue(preset);
	const kimi = row({ id: 'kimi', provider_type: 'kimi', auth_mode: 'plan_key', catalog_binding: 'kimi-binding', models: [model] });
	const opened = render(ModelCuration, { row: kimi });
	await screen.findByRole('checkbox', { name: model.label });
	await toggle(model.label);
	expect(saved()).toEqual([]);
	await opened.unmount();

	render(ModelCuration, { row: { ...kimi, models: saved() } });
	const choice = await screen.findByRole('checkbox', { name: model.label });
	expect((choice as HTMLInputElement).checked).toBe(false);
	expect(screen.queryByRole('button', { name: 'Refresh model list' })).toBeNull();
	expect(screen.queryByTestId('curation-typed')).toBeNull();
	mocks.patch.mockClear();
	await toggle(model.label);
	expect(saved()).toEqual([model]);
});

it('re-ticks a preset model once every model is unticked (#1524)', async () => {
	// A refreshable provider whose catalog is a built-in roster (MiniMax, Antigravity).
	const models = [entry('m-1'), entry('m-2')];
	mocks.post.mockResolvedValue({ source: 'preset', binding: 'mm-binding', models });
	const minimax = row({ id: 'mm', provider_type: 'minimax', catalog_binding: 'mm-binding', models: [] });
	render(ModelCuration, { row: minimax });
	// Nothing is curated, so the pickers offer nothing: no box reads ticked.
	const first = await screen.findByRole('checkbox', { name: 'm-1' });
	expect((first as HTMLInputElement).checked).toBe(false);
	expect((screen.getByRole('checkbox', { name: 'm-2' }) as HTMLInputElement).checked).toBe(false);
	await toggle('m-1');
	expect(saved()).toEqual([entry('m-1')]);
});

it("names the worker that captured an agent's list as the app does elsewhere", async () => {
	// A Docker worker's host name is its container ID; the app calls it "Local".
	mocks.installs = [{ id: 'dev-self', device_name: '75c733a3514a', is_self: true }];
	const models = [entry('gemini-4-argon')];
	mocks.get.mockResolvedValue({
		source: 'agent', binding: 'ag-binding', models, agent_version: '1.5.6',
		fetched_at: '2026-11-14T18:13:08Z', fetched_by_device: 'dev-self', fetched_by_name: '75c733a3514a'
	});
	render(ModelCuration, { row: row({ id: 'ag', provider_type: 'antigravity', catalog_binding: 'ag-binding', models }) });
	const line = await screen.findByTestId('curation-agent-list');
	await waitFor(() => expect(line.textContent).toMatch(/ on Local\. Refresh or a new chat updates it\.$/));
	expect(line.textContent).toMatch(/^Listed by the agent 1\.5\.6 /);
	expect(line.textContent).not.toContain('75c733a3514a');
});

it("asks the agent on opening when nothing it listed is stored (the old stored seed reads as nothing)", async () => {
	const seeded = [entry('gemini-4-argon')];
	mocks.post.mockResolvedValue({
		source: 'agent', binding: 'ag-binding', models: [...seeded, entry('gemini-4-hydrogen'), entry('claude-opus-5-5')],
		agent_version: '1.5.9', fetched_at: '2026-11-14T23:32:00Z', fetched_by_device: 'dev-self', fetched_by_name: 'Local'
	});
	render(ModelCuration, { row: row({ id: 'ag', provider_type: 'antigravity', catalog_binding: 'ag-binding', models: seeded }) });
	expect(await screen.findByRole('checkbox', { name: 'gemini-4-hydrogen' })).toBeTruthy();
	expect(mocks.get).toHaveBeenCalledWith('/model-providers/ag/model-catalog');
	expect(mocks.post).toHaveBeenCalledTimes(1);
	expect(mocks.post).toHaveBeenCalledWith('/model-providers/ag/fetch-models');
	// Listed, not ticked: curation stays the owner's.
	expect((screen.getByRole('checkbox', { name: 'claude-opus-5-5' }) as HTMLInputElement).checked).toBe(false);
	expect((screen.getByRole('checkbox', { name: 'gemini-4-argon' }) as HTMLInputElement).checked).toBe(true);
	expect(mocks.patch).not.toHaveBeenCalled();
});

describe('while the list is loading', () => {
	const seeded = [entry('gemini-4-argon'), entry('gemini-3.8-flash')];
	const agent = (models = seeded) => row({ id: 'ag', provider_type: 'antigravity', catalog_binding: 'ag-binding', models });
	let finish: (v: unknown) => void = () => {};
	beforeEach(() => {
		mocks.post.mockImplementation((path: string) =>
			path.endsWith('/fetch-models') ? new Promise((r) => { finish = r; }) : Promise.resolve({}));
	});
	afterEach(() => finish({ source: 'preset', binding: 'ag-binding', models: [] }));

	it('keeps a model unticked mid-fetch on screen, and lets it be ticked again (#1650)', async () => {
		render(ModelCuration, { row: agent() });
		await screen.findByTestId('curation-fetching');
		await toggle('gemini-3.8-flash');
		expect(saved()).toEqual([entry('gemini-4-argon')]);
		const flash = screen.getByRole('checkbox', { name: 'gemini-3.8-flash' }) as HTMLInputElement;
		expect(flash.checked).toBe(false);
		mocks.patch.mockClear();
		await fireEvent.click(flash);
		await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
		expect(saved().map((m: ProviderModelEntry) => m.id).sort()).toEqual(['gemini-3.8-flash', 'gemini-4-argon']);
		expect(screen.getByTestId('curation-fetching')).toBeTruthy();
	});

	it('keeps the unticked row when the saved row comes back mid-fetch (#1650)', async () => {
		const view = render(ModelCuration, { row: { ...agent(), revision: 'r1' } });
		await screen.findByTestId('curation-fetching');
		await toggle('gemini-3.8-flash');
		// The store echoes the saved row (new revision, the model gone) before the list arrives.
		await view.rerender({ row: { ...agent([entry('gemini-4-argon')]), revision: 'r2' } });
		const flash = await screen.findByRole('checkbox', { name: 'gemini-3.8-flash' }) as HTMLInputElement;
		expect(flash.checked).toBe(false);
		expect((screen.getByRole('checkbox', { name: 'gemini-4-argon' }) as HTMLInputElement).checked).toBe(true);
		finish({ source: 'agent', binding: 'ag-binding', models: seeded });
		await waitFor(() => expect(screen.queryByTestId('curation-fetching')).toBeNull());
		expect((screen.getByRole('checkbox', { name: 'gemini-3.8-flash' }) as HTMLInputElement).checked).toBe(false);
	});

	it('asks again when the key changed while the list was loading (S2)', async () => {
		const view = render(ModelCuration, { row: agent() });
		await screen.findByTestId('curation-fetching');
		// Reconnected to another account while the old fetch runs: its models go.
		await view.rerender({ row: { ...agent([entry('gemini-5-new')]), catalog_binding: 'ag-binding-b' } });
		await waitFor(() => expect(screen.queryByRole('checkbox', { name: 'gemini-3.8-flash' })).toBeNull());
		const fetches = () => mocks.post.mock.calls.filter(([p]) => String(p).endsWith('/fetch-models')).length;
		const before = fetches();
		finish({ source: 'agent', binding: 'ag-binding', models: seeded });
		// The old account's answer is not shown; the new account is asked.
		await waitFor(() => expect(fetches()).toBeGreaterThan(before));
		expect(screen.queryByRole('checkbox', { name: 'gemini-3.8-flash' })).toBeNull();
		finish({ source: 'agent', binding: 'ag-binding-b', models: [entry('gemini-5-new'), entry('gemini-5-other')] });
		expect(await screen.findByRole('checkbox', { name: 'gemini-5-other' })).toBeTruthy();
		expect((screen.getByRole('checkbox', { name: 'gemini-5-new' }) as HTMLInputElement).checked).toBe(true);
	});

	it('asks again when another Google account signed in on the same binding (S2)', async () => {
		const signedIn = (tag: string, models = seeded) => ({ ...agent(models), catalog_signin: tag });
		const view = render(ModelCuration, { row: signedIn('tag-a') });
		await screen.findByTestId('curation-fetching');
		// Account B signed in while A's fetch runs; the binding stays the same.
		await view.rerender({ row: signedIn('tag-b', [entry('gemini-5-new')]) });
		const fetches = () => mocks.post.mock.calls.filter(([p]) => String(p).endsWith('/fetch-models')).length;
		const before = fetches();
		finish({ source: 'agent', binding: 'ag-binding', signin: 'tag-a', models: seeded });
		// Account A's list is not shown for B; B is asked.
		await waitFor(() => expect(fetches()).toBeGreaterThan(before));
		expect(screen.queryByRole('checkbox', { name: 'gemini-3.8-flash' })).toBeNull();
		finish({ source: 'agent', binding: 'ag-binding', signin: 'tag-b', models: [entry('gemini-5-new'), entry('gemini-5-other')] });
		expect(await screen.findByRole('checkbox', { name: 'gemini-5-other' })).toBeTruthy();
	});

	it('shows a running fetch, still after leaving and coming back, and what it ended with (#1649)', async () => {
		const first = render(ModelCuration, { row: agent() });
		expect((await screen.findByTestId('curation-fetching')).textContent).toBe('Fetching list');
		expect(screen.getByTestId('curation-list-pending')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Refresh model list' }).getAttribute('aria-busy')).toBe('true');
		first.unmount(); // another tab
		render(ModelCuration, { row: agent() });
		expect(await screen.findByTestId('curation-fetching')).toBeTruthy();
		const refresh = screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement;
		expect(refresh.disabled).toBe(true);
		// Joined, not a second fetch.
		expect(mocks.post.mock.calls.filter(([p]) => String(p).endsWith('/fetch-models'))).toHaveLength(1);
		// The agent gave no list: the built-in models stay, and the tab says so.
		finish({ source: 'preset', binding: 'ag-binding', models: seeded });
		expect((await screen.findByTestId('curation-agent-silent')).textContent).toMatch(/has not listed its models yet/);
		expect(screen.queryByTestId('curation-fetching')).toBeNull();
		expect(refresh.disabled).toBe(false);
	});
});

describe('a ticked model the agent no longer lists (#1634)', () => {
	const listed = (source: string) => ({
		source, binding: 'ag-binding', models: [entry('gemini-4-hydrogen')], agent_version: '1.5.9',
		fetched_at: '2026-11-14T23:32:00Z', fetched_by_device: 'dev-self', fetched_by_name: 'Local'
	});
	const ticked = [entry('gemini-4-argon'), entry('gemini-4-hydrogen')];

	it('is marked on an agent-listed row, and the mark is never saved', async () => {
		mocks.get.mockResolvedValue(listed('agent'));
		render(ModelCuration, { row: row({ id: 'ag', provider_type: 'antigravity', catalog_binding: 'ag-binding', models: ticked }) });
		const marks = await screen.findAllByTestId('curation-unlisted');
		expect(marks).toHaveLength(1);
		expect(marks[0].closest('label')?.textContent).toContain('gemini-4-argon');
		expect((screen.getByRole('checkbox', { name: /gemini-4-argon/ }) as HTMLInputElement).checked).toBe(true);
		await toggle('gemini-4-hydrogen');
		expect(saved()).toEqual([entry('gemini-4-argon')]);
	});

	it('is not marked on a row still on the preset list', async () => {
		mocks.get.mockResolvedValue(listed('preset'));
		render(ModelCuration, { row: row({ id: 'ag', provider_type: 'antigravity', catalog_binding: 'ag-binding', models: ticked }) });
		await screen.findByRole('checkbox', { name: 'gemini-4-argon' });
		expect(screen.queryByTestId('curation-unlisted')).toBeNull();
	});
});

describe('shared catalog and concurrent curation', () => {
	const catalog = { source: 'upstream', binding: 'fixture-binding-a', models: [entry('old'), entry('remote'), entry('new')] };


	it.each([false, true])('setup validates the current key live instead of trusting a cached catalog (wizard=%s)', async (wizard) => {
		mocks.get.mockResolvedValue(catalog);
		mocks.post.mockRejectedValue(new Error('Could not fetch models from the provider upstream: http_401'));
		const onRejected = vi.fn();
		render(ModelCuration, { mode: 'setup', providerId: 'router', wizard, onRejected });
		await screen.findByRole('alert');
		expect(mocks.post).toHaveBeenCalledWith('/model-providers/router/fetch-models');
		expect(mocks.get).not.toHaveBeenCalled();
		expect(screen.queryByRole('checkbox')).toBeNull();
		if (wizard) expect(onRejected).toHaveBeenCalled();
		expect((screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement).disabled).toBe(true);
		expect(mocks.patch).not.toHaveBeenCalled();
	});

	it.each([false, true])('keeps full live discovery across resource echoes when sharing is compacted/skipped (shared=%s)', async (shared) => {
		const old = { source: 'upstream', binding: 'fixture-binding-a', models: [entry('old')], fetched_at: '2026-10-29T12:00:00Z' };
		const fetched_at = shared ? '2026-10-29T12:01:00Z' : null;
		mocks.get.mockResolvedValue(old);
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('new', { pricing: { prompt: '0.1' } })], fetched_at });
		render(ModelCuration, { row: router([entry('old')]) });
		await screen.findByRole('checkbox', { name: 'old' });
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
		await screen.findByRole('checkbox', { name: 'new' });
		mocks.get.mockResolvedValue(shared ? { source: 'upstream', binding: 'fixture-binding-a', models: [entry('new')], fetched_at } : old);
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		await toggle('new');
		expect(saved().find((m: ProviderModelEntry) => m.id === 'new').pricing).toEqual({ prompt: '0.1' });
	});

	it('keeps rich unshared discovery across a null-snapshot echo of the same proven binding', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('unshared', { pricing: { prompt: '0.1' } })], fetched_at: null });
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('checkbox', { name: 'unshared' });
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		await toggle('unshared');
		expect(saved()[0].pricing).toEqual({ prompt: '0.1' });
	});

	it.each([false, true])('drops unshared old-key models when the binding changes (catalog event first=%s)', async (catalogFirst) => {
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('old-key-only')], fetched_at: null });
		const view = render(ModelCuration, { row: router([]) });
		await screen.findByRole('checkbox', { name: 'old-key-only' });
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		if (catalogFirst) {
			resourceSync.invalidate(['settings']);
			await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		}
		await view.rerender({ row: { ...router([]), catalog_binding: 'fixture-binding-b', revision: 'new-key' } });
		if (!catalogFirst) {
			resourceSync.invalidate(['settings']);
			await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		}
		await waitFor(() => expect(screen.queryByRole('checkbox', { name: 'old-key-only' })).toBeNull());
		expect(mocks.post).toHaveBeenCalledTimes(1);
		expect(mocks.patch).not.toHaveBeenCalled();
	});

	it('discards an in-flight old-binding response after the provider key changes', async () => {
		let finish!: (value: typeof catalog) => void;
		mocks.post.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
		const view = render(ModelCuration, { row: router([]) });
		await waitFor(() => expect(mocks.post).toHaveBeenCalled());
		await view.rerender({ row: { ...router([]), catalog_binding: 'fixture-binding-b', revision: 'new-key' } });
		finish(catalog);
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		expect(screen.queryByRole('checkbox', { name: 'new' })).toBeNull();
		expect(mocks.patch).not.toHaveBeenCalled();
	});

	it.each([false, true])('shows catalog B once binding B catches up (catalog first=%s)', async (catalogFirst) => {
		mocks.get.mockResolvedValueOnce(catalog);
		const view = render(ModelCuration, { row: router([entry('old')]) });
		await screen.findByRole('checkbox', { name: 'new' });
		const incoming = { source: 'upstream', binding: 'fixture-binding-b', models: [entry('remote-b'), entry('new-b')] };
		let finish!: (value: typeof incoming) => void;
		mocks.get.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		if (catalogFirst) {
			finish(incoming);
			await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
			expect(screen.queryByRole('checkbox', { name: 'new-b' })).toBeNull();
		}
		await view.rerender({ row: { ...router([entry('remote-b')]), catalog_binding: 'fixture-binding-b', revision: 'binding-b-revision' } });
		if (!catalogFirst) finish(incoming);
		await screen.findByRole('checkbox', { name: 'new-b' });
		expect((screen.getByRole('checkbox', { name: 'remote-b' }) as HTMLInputElement).checked).toBe(true);
		expect(screen.queryByRole('checkbox', { name: 'old' })).toBeNull();
		await toggle('new-b');
		expect(saved().map((model: ProviderModelEntry) => model.id)).toEqual(['remote-b', 'new-b']);
		expect(mocks.patch.mock.calls.at(-1)![1].expected_revision).toBe('binding-b-revision');
		expect(mocks.get).toHaveBeenCalledTimes(2);
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('does not resurrect an old selection when a remote prop changes during refresh', async () => {
		let finish!: (value: typeof catalog) => void;
		mocks.post.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
		const view = render(ModelCuration, { row: router([entry('old')]) });
		await waitFor(() => expect(mocks.post).toHaveBeenCalled());
		await view.rerender({ row: { ...router([entry('remote')]), revision: 'remote-revision' } });
		finish(catalog);
		await screen.findByRole('checkbox', { name: 'new' });
		await waitFor(() => expect((screen.getByRole('checkbox', { name: 'remote' }) as HTMLInputElement).checked).toBe(true));
		expect((screen.getByRole('checkbox', { name: 'old' }) as HTMLInputElement).checked).toBe(false);
		await toggle('new');
		expect(saved().map((m: ProviderModelEntry) => m.id)).toEqual(['remote', 'new']);
		expect(mocks.patch.mock.calls.at(-1)![1].expected_revision).toBe('remote-revision');
	});

	it('renders the stored snapshot without fetching upstream or auto-selecting new models', async () => {
		mocks.get.mockResolvedValue({ ...catalog, fetched_at: '2026-10-29T12:00:00Z',
			fetched_by_device: 'worker-a', fetched_by_name: 'Fixture worker A' });
		render(ModelCuration, { row: router([entry('remote')]) });
		expect(await screen.findByText((_, node) => node?.tagName === 'P' && /Last refreshed .* on Fixture worker A/.test(node.textContent ?? ''))).toBeTruthy();
		expect((screen.getByRole('checkbox', { name: 'remote' }) as HTMLInputElement).checked).toBe(true);
		expect((screen.getByRole('checkbox', { name: 'new' }) as HTMLInputElement).checked).toBe(false);
		expect(mocks.post).not.toHaveBeenCalled();
	});


	it('reads remote settings live without an upstream fetch and reconciles the later provider prop', async () => {
		mocks.get.mockResolvedValueOnce({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('old')] });
		const view = render(ModelCuration, { row: { ...router([entry('old')]), revision: 'before' } });
		await screen.findByRole('checkbox', { name: 'old' });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
		mocks.get.mockResolvedValue(catalog);
		resourceSync.invalidate(['settings']);
		await screen.findByRole('checkbox', { name: 'new' });
		await view.rerender({ row: { ...router([entry('remote')]), revision: 'after' } });
		await waitFor(() => expect((screen.getByRole('checkbox', { name: 'remote' }) as HTMLInputElement).checked).toBe(true));
		expect((screen.getByRole('checkbox', { name: 'old' }) as HTMLInputElement).checked).toBe(false);
		expect((screen.getByRole('checkbox', { name: 'new' }) as HTMLInputElement).checked).toBe(false);
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('sends the displayed revision and reloads after a refused stale save', async () => {
		mocks.get.mockResolvedValue(catalog);
		mocks.post.mockResolvedValue(catalog);
		mocks.patch.mockRejectedValueOnce({ status: 409, message: 'Models changed on another worker' });
		const view = render(ModelCuration, { row: { ...router([entry('old')]), revision: 'old-revision' } });
		mocks.refresh.mockImplementationOnce(async () => {
			await view.rerender({ row: { ...router([entry('remote')]), revision: 'current-revision' } });
		});
		await toggle('new');
		expect(mocks.patch.mock.calls.at(-1)![1].expected_revision).toBe('old-revision');
		await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
		await waitFor(() => expect((screen.getByRole('checkbox', { name: 'remote' }) as HTMLInputElement).checked).toBe(true));
		expect((screen.getByRole('checkbox', { name: 'old' }) as HTMLInputElement).checked).toBe(false);
		expect(mocks.patch).toHaveBeenCalledTimes(1);
	});
});

describe('Models tab', () => {
	it('keeps normalized saved evidence when a refresh only returns stale raw fields', async () => {
		const canonical = { version: 1 as const, effort_levels: ['low', 'high', 'max'], default_effort: 'max' };
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('keep', stale), entry('toggle')] });
		render(ModelCuration, { row: router([entry('keep', { reasoning_capabilities: canonical })]) });
		await toggle('toggle');
		expect(mocks.patch).toHaveBeenCalledWith('/model-providers/router', expect.anything());
		expect(saved()[0].reasoning_capabilities).toEqual(canonical);
	});

	it('replaces stale metadata with explicit fresh empties and keeps stored extras checked', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('fresh'), entry('toggle')] });
		render(ModelCuration, { row: router([entry('fresh', stale), entry('extra')]) });
		await toggle('toggle');
		expect(saved()).toEqual([entry('extra'), entry('fresh'), entry('toggle')]);
	});

	it('keeps missing raw fields from storage while replacing the fresh ones', async () => {
		mocks.post.mockResolvedValue({
			source: 'upstream',
			binding: 'fixture-binding-a',
			models: [{ id: 'partial', label: 'partial', supported_reasoning_levels: [] }, entry('toggle')]
		});
		render(ModelCuration, { row: router([entry('partial', stale)]) });
		await toggle('toggle');
		expect(saved()[0]).toEqual({ ...entry('partial', stale), supported_reasoning_levels: [] });
	});

	it('keeps the stored curation checked when the fetch fails', async () => {
		mocks.post.mockRejectedValue(new Error('offline'));
		render(ModelCuration, { row: router([entry('keep'), entry('toggle')]) });
		const refresh = await screen.findByRole('button', { name: 'Refresh model list' });
		await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(refresh);
		expect((await screen.findByRole('alert')).textContent).toBe('offline');
		await toggle('toggle');
		expect(saved()).toEqual([entry('keep')]);
	});

	it('keeps a failed fetch on opening quiet; Refresh says why and a later success clears it (#1524)', async () => {
		mocks.post.mockRejectedValue(new Error('offline'));
		render(ModelCuration, { row: router([entry('keep')]) });
		await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(1));
		const refresh = await screen.findByRole('button', { name: 'Refresh model list' });
		await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));
		expect(screen.queryByRole('alert')).toBeNull();
		expect((screen.getByRole('checkbox', { name: 'keep' }) as HTMLInputElement).checked).toBe(true);
		await fireEvent.click(refresh);
		expect((await screen.findByRole('alert')).textContent).toBe('offline');
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('keep')] });
		await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(refresh);
		await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
	});

	it('keeps each model’s context window and pricing', async () => {
		const model = { ...entry('saved'), context_window: 123, pricing: { prompt: '0.1' } };
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('saved'), entry('other')] });
		render(ModelCuration, { row: router([model]) });
		await toggle('other');
		expect(saved()[0]).toEqual({ ...entry('saved'), context_window: 123, pricing: { prompt: '0.1' } });
	});

	it('rolls a failed save back and says so', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('a')] });
		mocks.patch.mockRejectedValueOnce(new Error('nope'));
		render(ModelCuration, { row: router([]) });
		await toggle('a');
		await waitFor(() => expect(mocks.notify).toHaveBeenCalledWith('nope', { severity: 'error' }));
		expect((screen.getByRole('checkbox', { name: 'a' }) as HTMLInputElement).checked).toBe(false);
	});

	it('lets a model name be typed in when the gateway has no model list (#1851)', async () => {
		mocks.post.mockRejectedValue(new Error('Could not fetch models from the provider upstream: http_404'));
		render(ModelCuration, { row: router([]) });
		await fireEvent.input(await screen.findByLabelText('Model name'), { target: { value: ' my-model ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
		await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
		expect(saved()).toEqual([{ id: 'my-model', label: 'my-model' }]);
		expect((screen.getByRole('checkbox', { name: 'my-model' }) as HTMLInputElement).checked).toBe(true);
	});

	it('offers typing for an empty list but not for a rejected key', async () => {
		mocks.post.mockResolvedValueOnce({ source: 'upstream', binding: 'fixture-binding-a', models: [] });
		render(ModelCuration, { row: router([]) });
		expect(await screen.findByLabelText('Model name')).toBeTruthy();
		cleanup();
		mocks.post.mockRejectedValueOnce(new Error('Could not fetch models from the provider upstream: http_401'));
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('alert');
		expect(screen.queryByLabelText('Model name')).toBeNull();
	});

	it('does not offer typing when the list has models', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('a')] });
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('checkbox', { name: 'a' });
		expect(screen.queryByLabelText('Model name')).toBeNull();
	});

 it('recovers an empty model filter through the shared Clear action', async () => {
  mocks.post.mockResolvedValue({ source: 'upstream', binding: 'fixture-binding-a', models: [entry('chat')] });
  render(ModelCuration, { row: router([]) });
  await screen.findByRole('checkbox', { name: 'chat' });
  const input = screen.getByLabelText('Filter models');
  await fireEvent.input(input, { target: { value: 'no-model-like-this' } });
  expect(screen.getByText('No models match “no-model-like-this”.')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
  expect((input as HTMLInputElement).value).toBe('');
  expect(screen.getByRole('checkbox', { name: 'chat' })).toBeTruthy();
 });
	it('explains a media model the filter finds in the hidden list', async () => {
		mocks.post.mockResolvedValue({
			source: 'upstream',
			binding: 'fixture-binding-a',
			models: [entry('chat')],
			hidden: [{ id: 'gpt-image-2', label: 'GPT Image 2', media_kind: 'image' }]
		});
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('checkbox', { name: 'chat' });
		await fireEvent.input(screen.getByLabelText('Filter models'), { target: { value: 'image' } });
		expect(await screen.findByText('gpt-image-2 is a image model')).toBeTruthy();
		expect(screen.getByRole('button', { name: /Default models/ })).toBeTruthy();
	});
});

describe('adding an API key', () => {
	const catalog = [
		{
			id: 'model-a',
			label: 'Model A',
			context_window: 200000,
			supported_reasoning_levels: ['low', 'high'],
			default_reasoning_level: 'high'
		},
		{ id: 'model-b', label: 'Model B' }
	];

	function start(providerId: 'openai' | 'grok' | 'openrouter') {
		const draft = providerDrafts.add(providerId, 'key', []);
		render(DraftHarness, { item: draft.item });
		return draft;
	}

	beforeEach(() => {
		mocks.post.mockImplementation(async (url: string) =>
			url.endsWith('/fetch-models') ? { source: 'upstream', binding: 'fixture-binding-a', models: catalog } : { id: 'new-provider' }
		);
	});

	it.each([
		['openai', 'openai', 'https://api.openai.com/v1', 'OpenAI API key'],
		['grok', 'xai', 'https://api.x.ai/v1', 'Grok API key'],
		['openrouter', 'openrouter', 'https://openrouter.ai/api', 'OpenRouter API key']
	] as const)('%s: picks a subset and keeps model metadata', async (providerId, type, base, name) => {
		start(providerId);
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: ' test-key ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.post).toHaveBeenCalledWith('/model-providers', {
			name,
			provider_type: type,
			base_url: base,
			auth_token: 'test-key'
		});
		expect(mocks.patch).not.toHaveBeenCalled();
		expect((screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox', { name: 'model-a Model A' }));
		await fireEvent.input(screen.getByLabelText('Filter models'), { target: { value: 'model-b' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.patch).toHaveBeenCalledWith('/model-providers/new-provider', { models: [catalog[0]] });
		expect(settingsNav.selectedItem('providers')).toBe('mp:new-provider');
	});

	it('inserts nothing above the list when it arrives, so the buttons stay put (#1648)', async () => {
		let finish: (v: unknown) => void = () => {};
		mocks.post.mockImplementation((url: string) =>
			url.endsWith('/fetch-models') ? new Promise((r) => { finish = r; }) : Promise.resolve({ id: 'new-provider' }));
		start('openai');
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByText('Fetching models');
		const card = () => document.querySelector('.setup-card') as HTMLElement;
		// What sits above the list box, and the buttons' place after it.
		const above = () => [...card().parentElement!.children]
			.slice(0, [...card().parentElement!.children].indexOf(card()))
			.map((el) => `${el.tagName}.${el.className}`);
		const before = above();
		expect((screen.getByLabelText('Filter models') as HTMLInputElement).disabled).toBe(true);
		finish({ source: 'agent', binding: 'fixture-binding-a', models: catalog, fetched_at: '2026-11-15T12:00:00Z' });
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(above()).toEqual(before);
		// The source is said on the line that was already there.
		expect(screen.getByText('The agent listed 2 models, pick what to offer')).toBeTruthy();
		expect(screen.queryByTestId('curation-agent-list')).toBeNull();
		expect((screen.getByLabelText('Filter models') as HTMLInputElement).disabled).toBe(false);
	});

	it('retries a failed catalog fetch without creating another row', async () => {
		let fetches = 0;
		mocks.post.mockImplementation(async (url: string) => {
			if (!url.endsWith('/fetch-models')) return { id: 'new-provider' };
			if (fetches++ === 0) throw new Error('Catalog unavailable');
			return { source: 'upstream', binding: 'fixture-binding-a', models: catalog };
		});
		start('openai');
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.post.mock.calls.filter(([url]) => url === '/model-providers')).toHaveLength(1);
	});

	it('keeps choices across a refresh and after a failed save', async () => {
		start('openai');
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await fireEvent.click(await screen.findByRole('checkbox', { name: 'model-a Model A' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
		await waitFor(() =>
			expect((screen.getByRole('checkbox', { name: 'model-a Model A' }) as HTMLInputElement).checked).toBe(true)
		);
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		mocks.patch.mockRejectedValueOnce(new Error('Save unavailable'));
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Save unavailable');
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.post.mock.calls.filter(([url]) => url === '/model-providers')).toHaveLength(1);
	});

	it('reuses the created row when refreshing the list fails', async () => {
		mocks.refresh.mockRejectedValueOnce(new Error('Refresh unavailable'));
		start('openai');
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.post.mock.calls.filter(([url]) => url === '/model-providers')).toHaveLength(1);
	});

	it('Choose later keeps the key with nothing offered', async () => {
		start('openrouter');
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'k' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		await fireEvent.click(screen.getByRole('button', { name: 'Choose later' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.patch).not.toHaveBeenCalled();
		expect(settingsNav.selectedItem('providers')).toBe('mp:new-provider');
	});

	it('Cancel before Continue drops the draft and creates nothing', async () => {
		start('openai');
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.post).not.toHaveBeenCalled();
		expect(settingsNav.selectedItem('providers')).toBe('provider:openai');
	});

	it('an Anthropic key is saved and selected without changing the Claude Code mode', async () => {
		mocks.addAnthropicAccount.mockResolvedValue('saved-key');
		const draft = providerDrafts.add('claude', 'anthropic', []);
		render(DraftHarness, { item: draft.item });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: ' sk-ant-x ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Use this key' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.addAnthropicAccount).toHaveBeenCalledWith('sk-ant-x', '');
		expect(settingsNav.selectedItem('providers')).toBe('claude:api-key:saved-key');
	});
});
