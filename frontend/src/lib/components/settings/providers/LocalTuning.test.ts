/**
 * A custom OpenAI-dialect gateway's Tuning tab (per-model local settings), and
 * the Overview saves that must keep those settings. Driven through the real
 * collection against the fake worker, so the asserted PATCH bodies are what the
 * API receives.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { LocalModelSettings, ModelProvider, ProviderModelEntry } from '$lib/api/model-providers';

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
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { gatewayRow, row } from './fixtures';
import { fakeWorker, type RouteHandler } from './fake-worker';
import { parseSampling, SAMPLING_FIELDS } from './local-tuning';

const caps = { version: 1 as const, effort_levels: [], default_effort: null };
const qwen: ProviderModelEntry = {
	id: 'qwen3-8b',
	label: 'Qwen3 8B',
	context_window: 32768,
	reasoning_capabilities: caps
};
const other: ProviderModelEntry = { id: 'llama-3.3', label: 'Llama 3.3', context_window: 8192 };
const tuned: LocalModelSettings = {
	server: 'vllm',
	preset: 'qwen3-thinking',
	sampling: { temperature: 0.5, repeat_penalty: 1.1 },
	reasoning: false
};

let worker: ReturnType<typeof fakeWorker>;

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

function start(rows: ModelProvider[], routes: Record<string, RouteHandler> = {}) {
	worker = fakeWorker({ rows }, routes);
	vi.stubGlobal('fetch', worker.fetch);
}

async function open(item: string, tab?: string) {
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item, ...(tab ? { tab } : {}) });
	render(ProvidersCollection);
	await flush();
}

const patches = () => worker.sent('PATCH', '/model-providers/gw-1');
const lastModels = () => (patches().at(-1)!.body as { models: ProviderModelEntry[] }).models;
const field = (name: string) => screen.getByRole('textbox', { name }) as HTMLInputElement;

async function type(name: string, value: string) {
	const input = field(name);
	await fireEvent.focus(input);
	await fireEvent.input(input, { target: { value } });
	await fireEvent.blur(input);
	await flush();
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('which credentials get a Tuning tab', () => {
	it.each([
		['custom gateway speaking OpenAI chat', gatewayRow(), true],
		['custom gateway speaking Anthropic Messages', gatewayRow({ api_dialect: 'anthropic' }), false],
		['custom gateway with no dialect (Anthropic)', gatewayRow({ api_dialect: null }), false]
	])('%s', async (_name, gw, shown) => {
		start([gw]);
		await open('mp:gw-1');
		expect(!!screen.queryByRole('tab', { name: 'Tuning' })).toBe(shown);
	});

	it('an OpenAI API key does not', async () => {
		start([row({ api_dialect: 'openai' })]);
		await open('mp:p1');
		expect(screen.getByRole('tab', { name: 'Usage' })).toBeTruthy();
		expect(screen.queryByRole('tab', { name: 'Tuning' })).toBeNull();
	});
});

describe('the Tuning tab', () => {
	it('with no models, points to the Models tab', async () => {
		start([gatewayRow({ models: [] })]);
		await open('mp:gw-1', 'Tuning');
		expect(screen.getByText(/No models saved yet/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Open Models' }));
		await flush();
		expect(screen.getByRole('tab', { name: 'Models' }).getAttribute('aria-selected')).toBe('true');
	});

	it('shows no tuning until a server is declared, then saves only the declaration', async () => {
		start([gatewayRow({ models: [qwen, other] })]);
		await open('mp:gw-1', 'Tuning');
		expect(screen.queryByRole('textbox', { name: 'Temperature' })).toBeNull();
		const server = screen.getByRole('group', { name: 'Server' });
		expect(within(server).getByRole('button', { name: 'Not set' }).getAttribute('aria-pressed')).toBe('true');

		await fireEvent.click(within(server).getByRole('button', { name: 'llama.cpp' }));
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()).toEqual([{ ...qwen, local_settings: { server: 'llama_cpp' } }, other]);
		await flush();
		// Blank fields keep the server default.
		expect(field('Temperature').value).toBe('');
		expect(field('Temperature').placeholder).toBe('Default');
	});

	it('refuses what the API refuses, without sending it', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: { server: 'llama_cpp' } }] })]);
		await open('mp:gw-1', 'Tuning');
		const cases: [string, string, RegExp][] = [
			['Temperature', '2.5', /Temperature must be 0 to 2/],
			['Top P', '-0.1', /Top P must be 0 to 1/],
			['Top K', '1.5', /Top K must be a whole number/],
			['Min P', 'abc', /Min P must be a number/],
			['Repeat penalty', '0', /Repeat penalty must be above 0/]
		];
		for (const [name, value, message] of cases) {
			await type(name, value);
			expect(screen.getByRole('alert').textContent).toMatch(message);
		}
		expect(patches()).toHaveLength(0);
		// Fixing one clears its message only.
		await type('Temperature', '0.6');
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()[0].local_settings).toEqual({ server: 'llama_cpp', sampling: { temperature: 0.6 } });
		expect(screen.getByRole('alert').textContent).not.toMatch(/Temperature/);
	});

	it('saves top_k 0, a preset and the thinking switch; a preset fills the placeholders', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: { server: 'vllm' } }] })]);
		await open('mp:gw-1', 'Tuning');
		await type('Top K', '0');
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()[0].local_settings).toEqual({ server: 'vllm', sampling: { top_k: 0 } });

		await fireEvent.change(screen.getByRole('combobox', { name: 'Preset' }), {
			target: { value: 'qwen3-non-thinking' }
		});
		await waitFor(() => expect(patches()).toHaveLength(2));
		expect(lastModels()[0].local_settings).toEqual({
			server: 'vllm',
			preset: 'qwen3-non-thinking',
			sampling: { top_k: 0 }
		});
		await flush();
		expect(field('Temperature').placeholder).toBe('0.7 preset');
		expect(screen.getByText('Preset turns it off')).toBeTruthy();

		await fireEvent.click(within(screen.getByRole('group', { name: 'Thinking' })).getByRole('button', { name: 'On' }));
		await waitFor(() => expect(patches()).toHaveLength(3));
		expect(lastModels()[0].local_settings).toMatchObject({ reasoning: true });
	});

	it('reset to server defaults keeps the declaration; Not set clears everything after a confirm', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }, other] })]);
		await open('mp:gw-1', 'Tuning');
		expect(field('Temperature').value).toBe('0.5');

		await fireEvent.click(screen.getByRole('button', { name: 'Reset to server defaults' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()).toEqual([{ ...qwen, local_settings: { server: 'vllm' } }, other]);
		await flush();
		expect(field('Temperature').value).toBe('');

		// Put an override back, then drop the declaration: asks first.
		await type('Temperature', '0.4');
		await waitFor(() => expect(patches()).toHaveLength(2));
		await fireEvent.click(within(screen.getByRole('group', { name: 'Server' })).getByRole('button', { name: 'Not set' }));
		expect(patches()).toHaveLength(2);
		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		await waitFor(() => expect(patches()).toHaveLength(3));
		expect(lastModels()).toEqual([qwen, other]);
		await flush();
		expect(screen.queryByRole('textbox', { name: 'Temperature' })).toBeNull();
	});

	it('tunes the picked model only', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }, other] })]);
		await open('mp:gw-1', 'Tuning');
		await fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), { target: { value: 'llama-3.3' } });
		await flush();
		expect(screen.queryByRole('textbox', { name: 'Temperature' })).toBeNull();
		await fireEvent.click(within(screen.getByRole('group', { name: 'Server' })).getByRole('button', { name: 'vLLM' }));
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()).toEqual([
			{ ...qwen, local_settings: tuned },
			{ ...other, local_settings: { server: 'vllm' } }
		]);
	});

	it("shows the API's refusal and keeps the stored value", async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: { server: 'llama_cpp' } }] })], {
			'PATCH /model-providers/:id': () => {
				throw { status: 400, detail: 'invalid local_settings.sampling.temperature' };
			}
		});
		await open('mp:gw-1', 'Tuning');
		await type('Temperature', '1');
		await waitFor(() =>
			expect(screen.getByRole('alert').textContent).toContain('Not saved: invalid local_settings.sampling.temperature')
		);
		expect(worker.state.rows[0].models[0].local_settings).toEqual({ server: 'llama_cpp' });
		// The field goes back to what is stored (blank: server default), not the refused value.
		await flush();
		expect(field('Temperature').value).toBe('');
	});

	it('stays on the model it just cleared instead of jumping to another tuned one', async () => {
		const b: ProviderModelEntry = { id: 'b-model', label: 'B', local_settings: { server: 'llama_cpp', sampling: { top_k: 5 } } };
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }, b, other] })]);
		await open('mp:gw-1', 'Tuning');
		const model = () => (screen.getByRole('combobox', { name: 'Model' }) as HTMLSelectElement).value;
		expect(model()).toBe('qwen3-8b');
		await fireEvent.click(within(screen.getByRole('group', { name: 'Server' })).getByRole('button', { name: 'Not set' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		await waitFor(() => expect(patches()).toHaveLength(1));
		await flush();
		expect(model()).toBe('qwen3-8b');
		expect(
			within(screen.getByRole('group', { name: 'Server' })).getByRole('button', { name: 'Not set' }).getAttribute('aria-pressed')
		).toBe('true');
	});

	it('marks a refused field invalid and links it to its message', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: { server: 'llama_cpp' } }] })]);
		await open('mp:gw-1', 'Tuning');
		expect(field('Top K').getAttribute('aria-invalid')).toBeNull();
		await type('Top K', '1e21');
		const input = field('Top K');
		expect(input.getAttribute('aria-invalid')).toBe('true');
		const ids = (input.getAttribute('aria-describedby') ?? '').split(' ');
		const message = ids.map((id) => document.getElementById(id)).find((el) => el?.textContent?.includes('Top K'));
		expect(message?.textContent).toMatch(/Top K must be a whole number/);
		expect(patches()).toHaveLength(0);
	});

	it('counts the Sampling group without the Thinking switch', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }] })]);
		await open('mp:gw-1', 'Tuning');
		// tuned: a preset and two values; its reasoning switch belongs to the Reasoning group.
		expect(screen.getByRole('heading', { name: /Sampling/ }).textContent).toMatch(/Sampling\s*3 set/);
	});
});

describe('Overview saves keep local tuning', () => {
	it('a rename sends every model back with its local_settings', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }, other] })]);
		await open('mp:gw-1');
		const name = screen.getByDisplayValue('ServerWorker vLLM');
		await fireEvent.focus(name);
		await fireEvent.input(name, { target: { value: 'Local llama' } });
		await fireEvent.blur(name);
		await waitFor(() => expect(patches()).toHaveLength(1));
		expect(lastModels()).toEqual([{ ...qwen, local_settings: tuned }, other]);
	});

	it('a dialect change with tuned models is stopped with a plain reason', async () => {
		start([gatewayRow({ models: [{ ...qwen, local_settings: tuned }] })]);
		await open('mp:gw-1');
		const dialect = screen.getByRole('group', { name: 'API dialect' });
		await fireEvent.click(within(dialect).getByRole('button', { name: 'Anthropic Messages' }));
		await flush();
		expect(patches()).toHaveLength(0);
		expect(screen.getByRole('alert').textContent).toMatch(/Clear the local tuning on qwen3-8b in the Tuning tab first/);
		expect(within(dialect).getByRole('button', { name: 'OpenAI chat' }).getAttribute('aria-pressed')).toBe('true');
	});
});

describe('parseSampling mirrors the API ranges', () => {
	const f = (key: string) => SAMPLING_FIELDS.find((x) => x.key === key)!;
	it.each([
		['temperature', '0', 0],
		['temperature', '2', 2],
		['top_p', '1', 1],
		['min_p', '0.05', 0.05],
		['top_k', '0', 0],
		['top_k', '40', 40],
		['repeat_penalty', '0.01', 0.01],
		['repeat_penalty', '1.1', 1.1],
		['temperature', '  ', null]
	])('%s %j → %j', (key, raw, value) => {
		expect(parseSampling(f(key), raw)).toEqual({ ok: true, value });
	});
	it.each([
		['temperature', '2.01'],
		['temperature', '-1'],
		['top_p', '1.5'],
		['min_p', '-0.5'],
		['top_k', '-1'],
		['top_k', '2.5'],
		['top_k', '1e21'],
		['repeat_penalty', '0'],
		['repeat_penalty', '-1'],
		['temperature', 'Infinity'],
		['temperature', 'NaN'],
		['temperature', '0x1'],
		['temperature', '1e999']
	])('%s %j is refused', (key, raw) => {
		expect(parseSampling(f(key), raw).ok).toBe(false);
	});
});
