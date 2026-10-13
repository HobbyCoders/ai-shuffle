/**
 * "Add provider": a custom gateway's setup in the draft detail. Name and
 * endpoint are required; Connect creates a `provider_type: 'custom'` row with
 * its dialect, then the models step picks what the picker offers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { ModelProvider } from '$lib/api/model-providers';

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
import ProvidersCollection from '../ProvidersCollection.svelte';
import { providerDrafts } from '../drafts.svelte';
import { startGateway } from '../provider-actions';
import { row } from '../fixtures';
import { fakeWorker } from '../fake-worker';

const catalog = [
	{ id: 'qwen3-coder', label: 'Qwen3 Coder', context_window: 131072 },
	{ id: 'llama-4', label: 'Llama 4' }
];

let worker: ReturnType<typeof fakeWorker>;
let createFails = false;

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function open() {
	await providers.refreshAll();
	render(ProvidersCollection);
	await flush();
	startGateway();
	await screen.findByRole('button', { name: 'Connect' });
}

const type = (label: string, value: string) => fireEvent.input(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	createFails = false;
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
	worker = fakeWorker(
		{ rows: [] },
		{
			'POST /model-providers': (body) => {
				if (createFails) throw { status: 400, detail: 'Endpoint did not answer' };
				const created = row({
					...(body as Partial<ModelProvider>),
					id: 'gw-new',
					auth_token_set: !!(body as { auth_token?: string }).auth_token
				});
				worker.state.rows = [...worker.state.rows, created];
				return created;
			},
			'POST /model-providers/:id/fetch-models': () => ({
				source: 'upstream',
				models: catalog
			})
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('GatewaySetup', () => {
	it('needs a name and an endpoint before it creates anything', async () => {
		await open();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Name and endpoint are both required.');
		await type('Name', 'Together AI');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect(screen.getByRole('alert').textContent).toBe('Name and endpoint are both required.');
		await type('Name', ' ');
		await type('Endpoint', 'https://api.together.xyz/v1');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect(screen.getByRole('alert').textContent).toBe('Name and endpoint are both required.');
		expect(worker.sent('POST', '/model-providers')).toHaveLength(0);
	});

	it('shows the typed name on the draft provider', async () => {
		await open();
		expect(screen.getByRole('group', { name: 'New provider' })).toBeTruthy();
		await type('Name', 'Together AI');
		await waitFor(() => expect(screen.getByRole('group', { name: 'Together AI' })).toBeTruthy());
		expect(screen.queryByRole('group', { name: 'New provider' })).toBeNull();
		await type('Name', '');
		await waitFor(() => expect(screen.getByRole('group', { name: 'New provider' })).toBeTruthy());
	});

	it('creates a custom row with its dialect and token, then picks its models', async () => {
		await open();
		await type('Name', ' Together AI ');
		await type('Endpoint', ' https://api.together.xyz/v1 ');
		await fireEvent.click(screen.getByRole('button', { name: 'Anthropic Messages' }));
		await type('Auth token', ' tok-123 ');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

		const box = await screen.findByRole('checkbox', { name: /^qwen3-coder/ });
		expect(worker.sent('POST', '/model-providers')).toHaveLength(1);
		expect(worker.sent('POST', '/model-providers')[0].body).toEqual({
			name: 'Together AI',
			provider_type: 'custom',
			base_url: 'https://api.together.xyz/v1',
			api_dialect: 'anthropic',
			auth_token: 'tok-123'
		});
		// The created row stays behind the draft until the setup finishes.
		expect(screen.getByRole('group', { name: 'Together AI' })).toBeTruthy();
		expect(
			within(screen.getByRole('group', { name: 'Together AI' })).getByRole('button', {
				name: 'Gateway key, Setting up'
			})
		).toBeTruthy();

		await fireEvent.click(box);
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() => expect(providerDrafts.list).toHaveLength(0));
		expect(worker.sent('PATCH', '/model-providers/gw-new')[0].body).toEqual({
			models: [catalog[0]]
		});
		expect(providerDrafts.gateways).toHaveLength(0);
		expect(settingsNav.selectedItem('providers')).toBe('mp:gw-new');
		await waitFor(() =>
			expect(
				within(screen.getByRole('group', { name: 'Together AI' })).getByRole('button', { name: 'Gateway key, On' })
			).toBeTruthy()
		);
	});

	it('defaults to the OpenAI chat dialect and sends no empty token', async () => {
		await open();
		await type('Name', 'Homelab vLLM');
		await type('Endpoint', 'http://10.0.0.20:8000/v1');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await screen.findByRole('checkbox', { name: /^qwen3-coder/ });
		expect(worker.sent('POST', '/model-providers')[0].body).toEqual({
			name: 'Homelab vLLM',
			provider_type: 'custom',
			base_url: 'http://10.0.0.20:8000/v1',
			api_dialect: 'openai'
		});
	});

	it('keeps the form and says why when the worker refuses', async () => {
		createFails = true;
		await open();
		await type('Name', 'Together AI');
		await type('Endpoint', 'https://api.together.xyz/v1');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Endpoint did not answer');
		expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('Together AI');
		expect(providerDrafts.list).toHaveLength(1);
	});

	it('Cancel drops the draft provider and creates nothing', async () => {
		await open();
		await type('Name', 'Together AI');
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(screen.queryByRole('group', { name: 'Together AI' })).toBeNull());
		expect(providerDrafts.list).toHaveLength(0);
		expect(providerDrafts.gateways).toHaveLength(0);
		expect(settingsNav.selectedItem('providers')).toBeNull();
		expect(worker.sent('POST', '/model-providers')).toHaveLength(0);
	});
});
