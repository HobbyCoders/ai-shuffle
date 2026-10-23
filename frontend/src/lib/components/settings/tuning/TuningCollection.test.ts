/**
 * Settings > Model tuning, driven through the real collection: the model
 * registry and provider rows come from the fake worker, and the account
 * preference sync is replaced by a recorder, so every assertion is on the
 * exact record the composer reads (`composer.modelEffort.<model>` and
 * friends) or the exact PATCH the model-providers API receives.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';

type Adapter = { read: () => unknown; apply: (v: unknown) => void };
const sync = vi.hoisted(() => ({
	adapters: new Map<string, Adapter>(),
	prefixes: new Map<string, (key: string) => Adapter | null>(),
	save: vi.fn()
}));
vi.mock('$lib/services/preferences', () => ({
	preferenceSync: {
		register: (key: string, adapter: Adapter) => sync.adapters.set(key, adapter),
		registerPrefix: (prefix: string, factory: (key: string) => Adapter | null) => sync.prefixes.set(prefix, factory),
		save: sync.save,
		reconnect: vi.fn(async () => {}),
		hydrate: vi.fn(async () => {})
	}
}));
const auth = vi.hoisted(() => ({ checkAuth: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: auth.checkAuth }
}));

import type { ModelProvider } from '$lib/api/model-providers';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';
import { providers } from '$lib/stores/providers.svelte';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { query } from '$lib/settings/search';
import { fakeWorker } from '../providers/fake-worker';
import { gatewayRow } from '../providers/fixtures';
import TuningCollection from './TuningCollection.svelte';
import { collectTuningModels, tuningItem } from './tuning-model';

const FIVE = ['low', 'medium', 'high', 'xhigh', 'max'];
const registry = {
	default: 'opus',
	legacy_map: {},
	claude: { auth_kind: 'subscription', plan_type: 'max' },
	models: [
		{
			value: 'opus',
			label: 'Opus',
			family: 'opus',
			context_window: 1_000_000,
			effort_levels: FIVE,
			default_effort: 'high',
			effort_metadata_version: 1
		}
	],
	providers: [
		{
			id: 'codex',
			name: 'ChatGPT',
			provider_type: 'chatgpt',
			auth_kind: 'subscription',
			plan_type: 'pro',
			default_model: 'gpt-6.1-sol',
			models: [
				{
					id: 'gpt-6.1-sol',
					label: 'GPT-6.1 Sol',
					effort_levels: [...FIVE, 'ultra'],
					default_effort: 'medium',
					initial_effort: 'high',
					effort_metadata_version: 1
				}
			]
		},
		{
			id: 'oai',
			name: 'OpenAI',
			provider_type: 'openai',
			auth_kind: 'api',
			default_model: null,
			models: [
				{ id: 'gpt-6.1-sol', label: 'GPT-6.1 Sol', effort_levels: FIVE, default_effort: 'medium', effort_metadata_version: 1 }
			]
		},
		{
			id: 'gw-1',
			name: 'Homelab vLLM',
			provider_type: 'custom',
			auth_kind: 'api',
			default_model: null,
			models: [{ id: 'qwen3-coder', label: 'Qwen3 Coder', effort_levels: [], effort_metadata_version: 1 }]
		},
		{
			id: 'or',
			name: 'OpenRouter',
			provider_type: 'openrouter',
			auth_kind: 'api',
			default_model: null,
			usable: false,
			models: [{ id: 'mystery/model', label: 'Mystery', effort_levels: null, effort_metadata_version: 1 }]
		}
	]
};

let worker: ReturnType<typeof fakeWorker>;
let rows: ModelProvider[];

async function flush(times = 8) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function open(key?: string) {
	await hydrateModelRegistry();
	await providers.refreshAll();
	settingsNav.clearSelection('tuning');
	settingsNav.go({ category: 'tuning', ...(key ? { item: tuningItem(key) } : {}) });
	render(TuningCollection);
	await flush();
}

/** Forget every per-model record, as another device clearing them would. */
function clearPrefs() {
	sync.adapters.get('composer.modelEfforts')?.apply(undefined);
	for (const [key, adapter] of sync.adapters) {
		if (/^composer\.model(Effort|Fast|ContextCap)\./.test(key)) adapter.apply(undefined);
	}
}

const combo = (name: string) => screen.getByRole('combobox', { name }) as HTMLSelectElement;
const lastSave = () => sync.save.mock.calls.at(-1);

beforeEach(() => {
	vi.clearAllMocks();
	auth.checkAuth.mockResolvedValue(undefined);
	rows = [gatewayRow({ models: [{ id: 'qwen3-coder', label: 'Qwen3 Coder', context_window: 131072 }] })];
	worker = fakeWorker({ rows }, {
		'GET /models': () => registry,
		'POST /model-providers/refresh-capabilities': () => ({ refreshed: [], failed: [] })
	});
	vi.stubGlobal('fetch', worker.fetch);
	providers.clear();
	clearPrefs();
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('the model list', () => {
	it('shows each picker model once, grouped like the picker, with shared routes counted', async () => {
		await open();
		const list = screen.getByRole('region', { name: 'Models' });
		const names = within(list).getAllByRole('button').map((b) => b.textContent?.replace(/\s+/g, ' ').trim());
		expect(names).toEqual([
			'GPT-6.1 Sol Effort High 2 routes',
			// The tile letter of a gateway without a brand mark comes first.
			'H Qwen3 Coder No adjustable settings',
			'Mystery Effort not known yet',
			'Opus Effort Auto (High)'
		]);
		expect(collectTuningModels().map((m) => m.key)).toEqual(['gpt-6.1-sol', 'qwen3-coder', 'mystery/model', 'claude-opus-5']);
	});

	it('is reachable from Settings search by model name and by "effort"', async () => {
		await hydrateModelRegistry();
		expect(query('GPT-6.1 Sol').some((r) => r.target.category === 'tuning' && r.target.item === tuningItem('gpt-6.1-sol'))).toBe(true);
		expect(query('reasoning effort').some((r) => r.target.category === 'tuning')).toBe(true);
	});
});

describe('effort', () => {
	it('starts a never-picked model at its measured level and saves a pick to the composer record', async () => {
		await open('gpt-6.1-sol');
		expect(combo('Effort').value).toBe('high');
		expect(screen.getByText('Starts at High, measured')).toBeTruthy();
		// The union of both routes' levels; Auto names the provider default.
		expect([...combo('Effort').options].map((o) => o.textContent)).toEqual([
			'Auto (Medium)', 'Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra'
		]);

		await fireEvent.change(combo('Effort'), { target: { value: 'low' } });
		await flush();
		expect(lastSave()).toEqual(['composer.modelEffort.gpt-6.1-sol', 'low']);
		expect(composerPreferences.effortForModel('prov:codex:gpt-6.1-sol')).toBe('low');
		expect(composerPreferences.effortForModel('prov:oai:gpt-6.1-sol')).toBe('low');
		expect(screen.getByText('Saved for new chats')).toBeTruthy();
	});

	it('shows the measured start when the OpenAI key route lists before ChatGPT', async () => {
		// Provider rows list newest first: an OpenAI key added after ChatGPT comes first.
		const [codex, oai, ...rest] = registry.providers;
		worker = fakeWorker({ rows }, {
			'GET /models': () => ({ ...registry, providers: [oai, codex, ...rest] }),
			'POST /model-providers/refresh-capabilities': () => ({ refreshed: [], failed: [] })
		});
		vi.stubGlobal('fetch', worker.fetch);
		await open('gpt-6.1-sol');
		const model = collectTuningModels().find((m) => m.key === 'gpt-6.1-sol')!;
		expect(model.routes.map((r) => r.value)).toEqual(['prov:oai:gpt-6.1-sol', 'prov:codex:gpt-6.1-sol']);

		// What a new chat on ChatGPT starts with, not the key route's Auto.
		expect(combo('Effort').value).toBe('high');
		expect(screen.getByText('Starts at High, measured')).toBeTruthy();
		const list = screen.getByRole('region', { name: 'Models' });
		expect(within(list).getByText(/Effort High/)).toBeTruthy();
		expect(within(list).queryByText('Changed')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'About Effort' }));
		expect(screen.getByRole('tooltip').textContent).toContain('That measured start is on OpenAI (PRO)');

		// Reset returns to that same start.
		await fireEvent.change(combo('Effort'), { target: { value: 'low' } });
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: /Reset Effort/i }));
		await flush();
		expect(lastSave()).toEqual(['composer.modelEffort.gpt-6.1-sol', 'high']);
		expect(within(list).queryByText('Changed')).toBeNull();
	});

	it('shows what each route runs when a level is not offered everywhere', async () => {
		await open('gpt-6.1-sol');
		await fireEvent.change(combo('Effort'), { target: { value: 'ultra' } });
		await flush();
		const routes = screen.getByRole('region', { name: /Routes/ });
		const text = routes.textContent?.replace(/\s+/g, ' ') ?? '';
		expect(text).toContain('Effort Ultra');
		expect(text).toContain('Effort Max');
	});

	it('Auto saves an explicit provider-default record, and reset goes back to the measured start', async () => {
		await open('gpt-6.1-sol');
		await fireEvent.change(combo('Effort'), { target: { value: '' } });
		await flush();
		expect(lastSave()).toEqual(['composer.modelEffort.gpt-6.1-sol', null]);
		expect(composerPreferences.effortForModel('prov:codex:gpt-6.1-sol')).toBeNull();
		expect(combo('Effort').value).toBe('');

		await fireEvent.click(screen.getByRole('button', { name: /Reset Effort/i }));
		await flush();
		expect(lastSave()).toEqual(['composer.modelEffort.gpt-6.1-sol', 'high']);
		expect(combo('Effort').value).toBe('high');
	});

	it('follows a change made on another device', async () => {
		await open('gpt-6.1-sol');
		await fireEvent.change(combo('Effort'), { target: { value: 'low' } });
		await flush();
		sync.adapters.get('composer.modelEffort.gpt-6.1-sol')!.apply('xhigh');
		await flush();
		expect(combo('Effort').value).toBe('xhigh');
	});

	it('round-trips: a saved pick is what the page shows when opened again', async () => {
		await open('gpt-6.1-sol');
		await fireEvent.change(combo('Effort'), { target: { value: 'medium' } });
		await flush();
		cleanup();
		await open('gpt-6.1-sol');
		expect(combo('Effort').value).toBe('medium');
		// The record the page reads back is the one the composer keeps.
		expect(sync.adapters.get('composer.modelEffort.gpt-6.1-sol')!.read()).toBe('medium');
	});

	it('says why effort cannot be set instead of offering a dead control', async () => {
		await open('mystery/model');
		expect(screen.queryByRole('combobox', { name: 'Effort' })).toBeNull();
		expect(screen.getByText('Not known yet')).toBeTruthy();
		expect(screen.getByText('Refresh models in Providers')).toBeTruthy();
		cleanup();
		await open('qwen3-coder');
		expect(screen.queryByRole('combobox', { name: 'Effort' })).toBeNull();
		expect(screen.getByText('Not adjustable')).toBeTruthy();
	});
});

describe('fast mode and the Standard window', () => {
	it('saves Fast per model and clears it on reset', async () => {
		await open('claude-opus-5');
		const fast = screen.getByRole('switch', { name: 'Fast mode' });
		expect(fast.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(fast);
		await flush();
		expect(lastSave()).toEqual(['composer.modelFast.claude-opus-5', true]);
		expect(composerPreferences.fastForModel('opus')).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: /Reset Fast mode/i }));
		await flush();
		expect(lastSave()).toEqual(['composer.modelFast.claude-opus-5', null]);
		expect(composerPreferences.fastForModel('opus')).toBeNull();
	});

	it('raises the Standard window for new chats, and the default clears the record', async () => {
		await open('claude-opus-5');
		const window = combo('Standard window');
		const values = [...window.options].map((o) => o.value);
		expect(values.length).toBeGreaterThan(1);
		const raised = values[1];
		await fireEvent.change(window, { target: { value: raised } });
		await flush();
		expect(lastSave()).toEqual(['composer.modelContextCap.claude-opus-5', Number(raised)]);
		expect(composerPreferences.contextCapForModel('opus')).toBe(Number(raised));
		await fireEvent.change(combo('Standard window'), { target: { value: values[0] } });
		await flush();
		expect(lastSave()).toEqual(['composer.modelContextCap.claude-opus-5', null]);
	});

	it('hides Fast and the window where a model has neither, and says why', async () => {
		await open('qwen3-coder');
		expect(screen.queryByRole('switch', { name: 'Fast mode' })).toBeNull();
		expect(screen.getAllByText('Not offered').length).toBeGreaterThan(0);
		expect(screen.getByText('No Fast tier')).toBeTruthy();
		expect(screen.queryByRole('combobox', { name: 'Standard window' })).toBeNull();
		expect(screen.getByText('Runs at its full window')).toBeTruthy();
	});
});

describe('local server tuning', () => {
	it('tunes a gateway model in place and PATCHes only its local_settings', async () => {
		await open('qwen3-coder');
		expect(screen.queryByRole('combobox', { name: 'Model' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'vLLM' }));
		await flush();
		const patch = worker.sent('PATCH', '/model-providers/gw-1').at(-1)!;
		expect((patch.body as { models: { id: string; local_settings?: unknown }[] }).models).toEqual([
			{ id: 'qwen3-coder', label: 'Qwen3 Coder', context_window: 131072, local_settings: { server: 'vllm' } }
		]);
		expect(worker.state.rows[0].models[0].local_settings).toEqual({ server: 'vllm' });
		// Declared: sampling now shows, as on the gateway's own Tuning tab.
		expect(screen.getByRole('textbox', { name: 'Temperature' })).toBeTruthy();
	});

	it('explains that hosted models take no sampling settings', async () => {
		await open('gpt-6.1-sol');
		expect(screen.getByText('Sampling and thinking')).toBeTruthy();
		expect(screen.getByText('Set by the provider')).toBeTruthy();
		expect(screen.queryByRole('textbox', { name: 'Temperature' })).toBeNull();
	});
});

it('marks changed models in the list', async () => {
	await open();
	const list = screen.getByRole('region', { name: 'Models' });
	expect(within(list).queryByText('Changed')).toBeNull();
	composerPreferences.rememberFast('opus', true);
	sync.adapters.get('composer.modelFast.claude-opus-5')!.apply(true);
	await flush();
	expect(within(list).getAllByText('Changed')).toHaveLength(1);
});
