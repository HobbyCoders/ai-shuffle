/**
 * The setup wizard's connect flow for key, sign-in and gateway providers,
 * rendered with the real Settings setup components and a faked worker API:
 * a model is required, a rejected key goes back to the key form and is
 * replaced on the same row, a timeout or server error offers Retry, and a
 * gateway with no model list offers Remove.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	post: vi.fn(),
	patch: vi.fn(),
	del: vi.fn(),
	refresh: vi.fn(),
	rows: [] as Record<string, unknown>[]
}));
vi.mock('$lib/api/client', () => ({
	api: { post: mocks.post, patch: mocks.patch, delete: mocks.del },
	getErrorMessage: (e: { message?: string } | undefined, fallback: string) => e?.message ?? fallback
}));
vi.mock('$lib/stores/providers.svelte', async () => {
	const api = await import('$lib/api/model-providers');
	return {
		providers: {
			get current() {
				return { providers: mocks.rows, claude: null };
			},
			fetchProviderModels: api.fetchProviderModels,
			refreshProviders: mocks.refresh,
			saveProvider: (data: Parameters<typeof api.updateModelProvider>[1], id: string) =>
				api.updateModelProvider(id, data),
			createGateway: (body: { name: string; base_url: string; api_dialect: string; auth_token?: string }) =>
				api.createModelProvider({ ...body, provider_type: 'custom', api_dialect: body.api_dialect as 'openai' })
		}
	};
});
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

import ProviderConnect from './ProviderConnect.svelte';
import { providerDrafts } from '$lib/components/settings/providers/drafts.svelte';

const catalog = [
	{ id: 'model-a', label: 'Model A' },
	{ id: 'model-b', label: 'Model B' }
];
const UPSTREAM = 'Could not fetch models from the provider upstream: ';

/** fetch-models answers, in order; then the catalog. */
function fetchAnswers(...answers: (Error | null)[]) {
	let n = 0;
	mocks.post.mockImplementation(async (url: string) => {
		if (url === '/model-providers') return { id: 'row-1' };
		if (url.endsWith('/fetch-models')) {
			const a = answers[n++];
			if (a) throw a;
			return { source: 'upstream', models: catalog, recommended };
		}
		return {};
	});
}
let recommended: string[] = ['model-a'];

const saveKey = async (value: string) => {
	await fireEvent.input(screen.getByLabelText('API key'), { target: { value } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
};
const continueOk = () => screen.queryByText(/^Connected/);

beforeEach(() => {
	vi.clearAllMocks();
	mocks.rows = [];
	mocks.patch.mockResolvedValue({});
	mocks.del.mockResolvedValue({ success: true });
	mocks.refresh.mockResolvedValue(undefined);
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	recommended = ['model-a'];
});
afterEach(cleanup);

describe('setup: connecting an API key', () => {
	it('a rejected key shows the reason, keeps the key form and never says Connected', async () => {
		fetchAnswers(new Error(`${UPSTREAM}http_401`));
		const onConnected = vi.fn();
		render(ProviderConnect, { provider: 'openrouter', onConnected });
		await saveKey('bad-key');
		const alert = await screen.findByTestId('setup-provider-rejected');
		expect(alert.textContent).toBe('That key was rejected by OpenRouter. Check it and try again.');
		const field = screen.getByLabelText('API key') as HTMLInputElement;
		expect(field.disabled).toBe(false);
		expect(continueOk()).toBeNull();
		expect(screen.queryByRole('button', { name: /Use selected/ })).toBeNull();
		expect(onConnected).not.toHaveBeenCalled();
	});

	it('replacing a rejected key reuses the same row, then saves the model and its default', async () => {
		fetchAnswers(new Error(`${UPSTREAM}http_403`), null);
		const onConnected = vi.fn();
		render(ProviderConnect, { provider: 'openrouter', onConnected });
		await saveKey('bad-key');
		await screen.findByTestId('setup-provider-rejected');
		await saveKey('good-key');
		expect(mocks.patch).toHaveBeenCalledWith('/model-providers/row-1', { auth_token: 'good-key' });
		// The roster's model is picked already, so one click continues.
		const first = (await screen.findByRole('checkbox', { name: 'model-a Model A' })) as HTMLInputElement;
		expect(first.checked).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() => expect(onConnected).toHaveBeenCalledWith('openrouter'));
		expect(mocks.post.mock.calls.filter(([url]) => url === '/model-providers')).toHaveLength(1);
		expect(mocks.del).not.toHaveBeenCalled();
		expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', {
			models: [{ id: 'model-a', label: 'Model A' }],
			default_model: 'model-a'
		});
	});

	it('a timeout or server error offers Retry, which fetches again', async () => {
		fetchAnswers(new Error(`${UPSTREAM}http_503`), new Error(`${UPSTREAM}ReadTimeout`), null);
		render(ProviderConnect, { provider: 'openrouter' });
		await saveKey('key');
		const failure = await screen.findByTestId('curation-failure');
		expect(failure.dataset.kind).toBe('transient');
		expect(failure.textContent).toContain('OpenRouter had a server error (HTTP 503). Retry in a moment.');
		expect(continueOk()).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() =>
			expect(screen.getByTestId('curation-failure').textContent).toContain('OpenRouter did not answer in time.')
		);
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(screen.queryByTestId('curation-failure')).toBeNull();
	});

	it('has no Choose later, and needs a model before it can finish', async () => {
		fetchAnswers(null);
		const onConnected = vi.fn();
		render(ProviderConnect, { provider: 'openrouter', onConnected });
		await saveKey('key');
		const first = await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(screen.queryByRole('button', { name: 'Choose later' })).toBeNull();
		await fireEvent.click(first);
		const use = screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement;
		expect(use.disabled).toBe(true);
		expect(onConnected).not.toHaveBeenCalled();
		expect(continueOk()).toBeNull();
	});

	it("pre-checks the provider's roster model, and saves it as the default", async () => {
		recommended = ['model-x', 'model-b'];
		const onConnected = vi.fn();
		render(ProviderConnect, { provider: 'openrouter', onConnected });
		await saveKey('key');
		const b = (await screen.findByRole('checkbox', { name: 'model-b Model B' })) as HTMLInputElement;
		expect(b.checked).toBe(true);
		expect((screen.getByRole('checkbox', { name: 'model-a Model A' }) as HTMLInputElement).checked).toBe(false);
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() => expect(onConnected).toHaveBeenCalledWith('openrouter'));
		expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', {
			models: [{ id: 'model-b', label: 'Model B' }],
			default_model: 'model-b'
		});
	});

	it('pre-checks nothing without a roster model, and asks the owner to pick', async () => {
		recommended = [];
		const onConnected = vi.fn();
		render(ProviderConnect, { provider: 'openrouter', onConnected });
		await saveKey('key');
		const a = (await screen.findByRole('checkbox', { name: 'model-a Model A' })) as HTMLInputElement;
		expect(a.checked).toBe(false);
		expect((screen.getByRole('checkbox', { name: 'model-b Model B' }) as HTMLInputElement).checked).toBe(false);
		expect(screen.getByTestId('curation-pick-hint').textContent).toBe('Pick a model to continue.');
		expect((screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement).disabled).toBe(true);
		// The default is the first checked model in display order.
		await fireEvent.click(screen.getByRole('checkbox', { name: 'model-b Model B' }));
		await fireEvent.click(a);
		expect(screen.queryByTestId('curation-pick-hint')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (2)' }));
		await waitFor(() => expect(onConnected).toHaveBeenCalledWith('openrouter'));
		expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', {
			models: [{ id: 'model-a', label: 'Model A' }, { id: 'model-b', label: 'Model B' }],
			default_model: 'model-a'
		});
	});

	it('keeps a default the row already has while it is still picked', async () => {
		fetchAnswers(null);
		mocks.rows = [{ id: 'row-1', default_model: 'model-a', models: [] }];
		render(ProviderConnect, { provider: 'openrouter' });
		await saveKey('key');
		await fireEvent.click(await screen.findByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() =>
			expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', { models: [{ id: 'model-a', label: 'Model A' }] })
		);
	});

	it('replaces a stale default that is no longer among the picked models', async () => {
		fetchAnswers(null);
		mocks.rows = [{ id: 'row-1', default_model: 'model-b', models: [] }];
		render(ProviderConnect, { provider: 'openrouter' });
		await saveKey('key');
		await fireEvent.click(await screen.findByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() =>
			expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', {
				models: [{ id: 'model-a', label: 'Model A' }],
				default_model: 'model-a'
			})
		);
	});

	it('says Connected only once the step reports the provider connected', async () => {
		fetchAnswers(null);
		const view = render(ProviderConnect, { provider: 'openrouter', connected: false });
		await saveKey('key');
		await fireEvent.click(await screen.findByRole('button', { name: 'Use selected (1)' }));
		const done = await screen.findByTestId('setup-provider-connect-done');
		expect(done.textContent).not.toContain('Connected');
		await view.rerender({ provider: 'openrouter', connected: true });
		expect(screen.getByTestId('setup-provider-connect-done').textContent).toContain('Connected.');
	});

	it('resumes a saved key that has no model instead of adding a second row', async () => {
		fetchAnswers(null);
		const pending = { id: 'old-row', name: 'OpenRouter API key', models: [] };
		render(ProviderConnect, { provider: 'openrouter', pending: pending as never });
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.post).toHaveBeenCalledWith('/model-providers/old-row/fetch-models');
		expect(mocks.post.mock.calls.filter(([url]) => url === '/model-providers')).toHaveLength(0);
	});
});

describe('setup: a gateway with no model list', () => {
	it('explains it and offers Remove, which deletes the row', async () => {
		fetchAnswers(new Error(`${UPSTREAM}http_404`));
		const onChanged = vi.fn();
		render(ProviderConnect, { provider: 'gateway', onChanged });
		await fireEvent.input(screen.getByPlaceholderText('Together AI'), { target: { value: 'Box' } });
		await fireEvent.input(screen.getByPlaceholderText('https://host/v1'), { target: { value: 'https://box/v1' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		const failure = await screen.findByTestId('curation-failure');
		expect(failure.dataset.kind).toBe('no-list');
		expect(failure.textContent).toContain('does not list its models');
		expect(continueOk()).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(mocks.del).toHaveBeenCalledWith('/model-providers/row-1'));
		await waitFor(() => expect(onChanged).toHaveBeenCalled());
	});

	it('is completed with a typed model name (#1851)', async () => {
		fetchAnswers(new Error(`${UPSTREAM}http_404`));
		render(ProviderConnect, { provider: 'gateway' });
		await fireEvent.input(screen.getByPlaceholderText('Together AI'), { target: { value: 'Box' } });
		await fireEvent.input(screen.getByPlaceholderText('https://host/v1'), { target: { value: 'https://box/v1' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		const failure = await screen.findByTestId('curation-failure');
		expect(failure.textContent).toContain('Type the model names it serves below.');
		expect((screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('Model name'), { target: { value: 'box-model' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() =>
			expect(mocks.patch).toHaveBeenLastCalledWith('/model-providers/row-1', {
				models: [{ id: 'box-model', label: 'box-model' }],
				default_model: 'box-model'
			})
		);
		expect(await screen.findByTestId('setup-provider-connect-done')).toBeTruthy();
	});
});

describe('setup: a subscription sign-in', () => {
	it.each(['openai', 'grok'] as const)('%s labels its subscription sign-in Unofficial, the API key not', (provider) => {
		render(ProviderConnect, { provider });
		const signIn = screen.getByRole('button', { name: /^Sign in with/ });
		expect(signIn.textContent).toContain('Unofficial');
		expect(signIn.textContent).toContain(
			'Uses your own subscription through an unofficial sign-in. The provider may change or block it.'
		);
		expect(screen.getByRole('button', { name: /^API key/ }).textContent).not.toContain('Unofficial');
	});
});
