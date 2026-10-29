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
	setAnthropicKey: vi.fn(),
	loadClaude: vi.fn()
}));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post, patch: mocks.patch },
	getErrorMessage: (e: { message?: string } | undefined, fallback: string) => e?.message ?? fallback
}));
vi.mock('$lib/stores/providers.svelte', async () => {
	const api = await import('$lib/api/model-providers');
	return {
		providers: {
			current: { providers: [], claude: null },
			fetchProviderModels: api.fetchProviderModels,
			saveProvider: (data: Parameters<typeof api.updateModelProvider>[1], id: string) =>
				api.updateModelProvider(id, data),
			refreshProviders: mocks.refresh,
			setAnthropicKey: mocks.setAnthropicKey,
			loadClaude: mocks.loadClaude
		}
	};
});
vi.mock('$lib/stores/notifications', () => ({ notify: mocks.notify }));

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

describe('shared catalog and concurrent curation', () => {
	const catalog = { source: 'upstream', models: [entry('old'), entry('remote'), entry('new')] };


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
		const old = { source: 'upstream', models: [entry('old')], fetched_at: '2026-10-29T12:00:00Z' };
		const fetched_at = shared ? '2026-10-29T12:01:00Z' : null;
		mocks.get.mockResolvedValue(old);
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('new', { pricing: { prompt: '0.1' } })], fetched_at });
		render(ModelCuration, { row: router([entry('old')]) });
		await screen.findByRole('checkbox', { name: 'old' });
		await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
		await screen.findByRole('checkbox', { name: 'new' });
		mocks.get.mockResolvedValue(shared ? { source: 'upstream', models: [entry('new')], fetched_at } : old);
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		await toggle('new');
		expect(saved().find((m: ProviderModelEntry) => m.id === 'new').pricing).toEqual({ prompt: '0.1' });
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
		mocks.get.mockResolvedValueOnce({ source: 'upstream', models: [entry('old')] });
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
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('keep', stale), entry('toggle')] });
		render(ModelCuration, { row: router([entry('keep', { reasoning_capabilities: canonical })]) });
		await toggle('toggle');
		expect(mocks.patch).toHaveBeenCalledWith('/model-providers/router', expect.anything());
		expect(saved()[0].reasoning_capabilities).toEqual(canonical);
	});

	it('replaces stale metadata with explicit fresh empties and keeps stored extras checked', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('fresh'), entry('toggle')] });
		render(ModelCuration, { row: router([entry('fresh', stale), entry('extra')]) });
		await toggle('toggle');
		expect(saved()).toEqual([entry('extra'), entry('fresh'), entry('toggle')]);
	});

	it('keeps missing raw fields from storage while replacing the fresh ones', async () => {
		mocks.post.mockResolvedValue({
			source: 'upstream',
			models: [{ id: 'partial', label: 'partial', supported_reasoning_levels: [] }, entry('toggle')]
		});
		render(ModelCuration, { row: router([entry('partial', stale)]) });
		await toggle('toggle');
		expect(saved()[0]).toEqual({ ...entry('partial', stale), supported_reasoning_levels: [] });
	});

	it('keeps the stored curation checked when the fetch fails', async () => {
		mocks.post.mockRejectedValue(new Error('offline'));
		render(ModelCuration, { row: router([entry('keep'), entry('toggle')]) });
		expect((await screen.findByRole('alert')).textContent).toBe('offline');
		await toggle('toggle');
		expect(saved()).toEqual([entry('keep')]);
	});

	it('keeps each model’s context window and pricing', async () => {
		const model = { ...entry('saved'), context_window: 123, pricing: { prompt: '0.1' } };
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('saved'), entry('other')] });
		render(ModelCuration, { row: router([model]) });
		await toggle('other');
		expect(saved()[0]).toEqual({ ...entry('saved'), context_window: 123, pricing: { prompt: '0.1' } });
	});

	it('rolls a failed save back and says so', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('a')] });
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
		mocks.post.mockResolvedValueOnce({ source: 'upstream', models: [] });
		render(ModelCuration, { row: router([]) });
		expect(await screen.findByLabelText('Model name')).toBeTruthy();
		cleanup();
		mocks.post.mockRejectedValueOnce(new Error('Could not fetch models from the provider upstream: http_401'));
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('alert');
		expect(screen.queryByLabelText('Model name')).toBeNull();
	});

	it('does not offer typing when the list has models', async () => {
		mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('a')] });
		render(ModelCuration, { row: router([]) });
		await screen.findByRole('checkbox', { name: 'a' });
		expect(screen.queryByLabelText('Model name')).toBeNull();
	});

	it('explains a media model the filter finds in the hidden list', async () => {
		mocks.post.mockResolvedValue({
			source: 'upstream',
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
			url.endsWith('/fetch-models') ? { source: 'upstream', models: catalog } : { id: 'new-provider' }
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

	it('retries a failed catalog fetch without creating another row', async () => {
		let fetches = 0;
		mocks.post.mockImplementation(async (url: string) => {
			if (!url.endsWith('/fetch-models')) return { id: 'new-provider' };
			if (fetches++ === 0) throw new Error('Catalog unavailable');
			return { source: 'upstream', models: catalog };
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

	it('an Anthropic key becomes the active credential when Claude has none', async () => {
		mocks.setAnthropicKey.mockResolvedValue(undefined);
		const draft = providerDrafts.add('claude', 'anthropic', []);
		render(DraftHarness, { item: draft.item });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: ' sk-ant-x ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Use this key' }));
		await screen.findByTestId('draft-gone');
		expect(mocks.setAnthropicKey).toHaveBeenCalledWith('sk-ant-x', { useForClaudeCode: true });
		expect(settingsNav.selectedItem('providers')).toBe('claude:api-key');
	});
});
