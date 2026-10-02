import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ create: vi.fn(), update: vi.fn(), fetchModels: vi.fn(), refresh: vi.fn(), integrations: vi.fn() }));
vi.mock('$lib/api/model-providers', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api/model-providers')>(),
	createModelProvider: mocks.create, updateModelProvider: mocks.update
}));
vi.mock('$lib/stores/providers.svelte', () => ({ providers: {
	fetchProviderModels: mocks.fetchModels, refreshProviders: mocks.refresh, loadIntegrations: mocks.integrations
} }));
vi.mock('$lib/api/runtime', () => ({ apiBase: () => '/api/v1', withAuth: (init: RequestInit) => init }));

import OpenAiKeyFlow from './OpenAiKeyFlow.svelte';
import GrokKeyFlow from './GrokKeyFlow.svelte';
import OpenRouterKeyFlow from './OpenRouterKeyFlow.svelte';
import FalAiKeyFlow from './FalAiKeyFlow.svelte';
import AddProviderModal from '../AddProviderModal.svelte';

const catalog = [
	{ id: 'model-a', label: 'Model A', context_window: 200000, supported_reasoning_levels: ['low', 'high'], default_reasoning_level: 'high' },
	{ id: 'model-b', label: 'Model B' }
];
beforeEach(() => {
	vi.resetAllMocks();
	mocks.create.mockResolvedValue({ id: 'new-provider' });
	mocks.update.mockResolvedValue({});
	mocks.fetchModels.mockResolvedValue({ models: catalog, source: 'upstream' });
	mocks.refresh.mockResolvedValue(undefined);
	mocks.integrations.mockResolvedValue(undefined);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('adding provider API keys', () => {
	it.each([
		['OpenAI', 'openai', OpenAiKeyFlow], ['Grok', 'xai', GrokKeyFlow], ['OpenRouter', 'openrouter', OpenRouterKeyFlow]
	] as const)('%s lets the user choose a subset and preserves model metadata', async (name, providerType, component) => {
		const onDone = vi.fn();
		render(component, { onDone, onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: ' test-key ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({ name, provider_type: providerType, auth_token: 'test-key' }));
		expect(mocks.update).not.toHaveBeenCalled();
		expect(onDone).not.toHaveBeenCalled();
		expect((screen.getByRole('button', { name: 'Use selected' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox', { name: 'model-a Model A' }));
		await fireEvent.input(screen.getByLabelText('Filter models'), { target: { value: 'model-b' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
		expect(mocks.update).toHaveBeenCalledWith('new-provider', { models: [catalog[0]] });
	});

	it('retries a failed catalog fetch without creating another provider', async () => {
		mocks.fetchModels.mockRejectedValueOnce(new Error('Catalog unavailable'));
		render(OpenAiKeyFlow, { onDone: vi.fn(), onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('keeps model choices on refresh and after a failed save', async () => {
		const onDone = vi.fn();
		render(OpenAiKeyFlow, { onDone, onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await fireEvent.click(await screen.findByRole('checkbox', { name: 'model-a Model A' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		expect((await screen.findByRole('checkbox', { name: 'model-a Model A' }) as HTMLInputElement).checked).toBe(true);
		mocks.update.mockRejectedValueOnce(new Error('Save unavailable'));
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await screen.findByRole('alert');
		expect(onDone).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Use selected (1)' }));
		await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
		expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('reuses the created provider if refreshing the list fails', async () => {
		mocks.refresh.mockRejectedValueOnce(new Error('Refresh unavailable'));
		render(OpenAiKeyFlow, { onDone: vi.fn(), onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('checkbox', { name: 'model-a Model A' });
		expect(mocks.create).toHaveBeenCalledOnce();
	});
});

describe('fal.ai setup', () => {
	it('is offered in Add provider and opens its key flow', async () => {
		render(AddProviderModal, { onClose: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: /fal.ai API key/ }));
		expect(screen.getByRole('dialog', { name: 'fal.ai API key' })).toBeTruthy();
		expect(screen.getByLabelText('API key')).toBeTruthy();
	});

	it('saves the media integration, refreshes status and continues to media models', async () => {
		const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ valid: true }) });
		vi.stubGlobal('fetch', fetchMock);
		const onDone = vi.fn();
		render(FalAiKeyFlow, { onDone, onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: ' fal-test-key ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('status');
		expect(fetchMock).toHaveBeenCalledWith('/api/v1/settings/integrations/fal-ai', expect.objectContaining({ method: 'POST', body: JSON.stringify({ api_key: 'fal-test-key' }) }));
		expect(mocks.integrations).toHaveBeenCalledOnce();
		expect(mocks.create).not.toHaveBeenCalled();
		expect(onDone).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
		expect(onDone).toHaveBeenCalledOnce();
	});

	it('keeps the key form open when saving fails', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: 'Could not save key' }) }));
		const onDone = vi.fn();
		render(FalAiKeyFlow, { onDone, onCancel: vi.fn() });
		await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Could not save key');
		expect(onDone).not.toHaveBeenCalled();
		expect(mocks.integrations).not.toHaveBeenCalled();
	});
});
