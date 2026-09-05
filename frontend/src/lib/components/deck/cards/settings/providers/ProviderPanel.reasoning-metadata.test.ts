import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { ModelProvider, ProviderModelEntry } from '$lib/api/model-providers';

const mocks = vi.hoisted(() => ({ post: vi.fn(), patch: vi.fn(), refresh: vi.fn() }));
vi.mock('$lib/api/client', () => ({
 api: { post: mocks.post, patch: mocks.patch },
 getErrorMessage: (_e: unknown, fallback: string) => fallback
}));
vi.mock('$lib/stores/providers.svelte', async () => {
 const api = await import('$lib/api/model-providers');
 return { providers: {
  fetchProviderModels: api.fetchProviderModels,
  saveProvider: (data: Parameters<typeof api.updateModelProvider>[1], id: string) => api.updateModelProvider(id, data),
  refreshProviders: mocks.refresh
 } };
});
vi.mock('$lib/constants/models.svelte', () => ({ getModels: () => [] }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));
import ProviderPanel from './ProviderPanel.svelte';
import OpenRouterKeyFlow from './flows/OpenRouterKeyFlow.svelte';

const raw = { supported_parameters: [], supported_reasoning_levels: [], default_reasoning_level: null };
const stale = { supported_parameters: ['reasoning'], supported_reasoning_levels: ['high'], default_reasoning_level: 'high' };
const entry = (id: string, metadata: Partial<ProviderModelEntry> = raw): ProviderModelEntry => ({ id, label: id, ...metadata });
const provider = (models: ProviderModelEntry[]): ModelProvider => ({
 id: 'router', name: 'OpenRouter', provider_type: 'openrouter', base_url: 'https://openrouter.ai/api',
 auth_token_set: true, auth_token_masked: '', models, default_model: null, small_fast_model: null,
 extra_env: {}, context_window: null, max_output_tokens: null, enabled: true, created_at: '', updated_at: ''
});
const toggle = async (id: string) => {
 await fireEvent.click(await screen.findByRole('checkbox', { name: id }));
 await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
};
const saved = () => mocks.patch.mock.calls.at(-1)![1].models;
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mocks.patch.mockResolvedValue({}); mocks.refresh.mockResolvedValue(undefined); });

describe('provider raw reasoning metadata round trips', () => {
 it('fetches through the API and keeps raw empty arrays/null when OpenRouter selection is saved', async () => {
  mocks.post.mockImplementation((url: string) => Promise.resolve(url.endsWith('/fetch-models')
   ? { source: 'upstream', models: [{ ...entry('fresh'), effort_levels: ['max'], default_effort: 'max' }] }
   : provider([])));
  render(OpenRouterKeyFlow, { onDone: vi.fn(), onCancel: vi.fn() });
  await fireEvent.input(screen.getByLabelText('API key'), { target: { value: 'test-key' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'fresh' }));
  await fireEvent.click(screen.getByRole('button', { name: /Use selected/ }));
  await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
  expect(mocks.post).toHaveBeenCalledWith('/model-providers/router/fetch-models');
  expect(saved()).toEqual([entry('fresh')]);
 });

 it('replaces stale catalog metadata with explicit fresh empties and preserves stored extras', async () => {
  mocks.post.mockResolvedValue({ source: 'upstream', models: [entry('fresh'), entry('toggle')] });
  render(ProviderPanel, { kind: 'key', provider: provider([entry('fresh', stale), entry('extra')]) });
  await toggle('toggle');
  expect(saved()).toEqual([entry('extra'), entry('fresh'), entry('toggle')]);
 });

 it('preserves missing raw fields from storage while replacing corresponding fresh fields', async () => {
  mocks.post.mockResolvedValue({ source: 'upstream', models: [
   { id: 'partial', label: 'partial', supported_reasoning_levels: [] }, entry('toggle')
  ] });
  render(ProviderPanel, { kind: 'key', provider: provider([entry('partial', stale)]) });
  await toggle('toggle');
  expect(saved()[0]).toEqual({ ...entry('partial', stale), supported_reasoning_levels: [] });
 });

 it('keeps raw metadata in stored fallback rows when the fetch fails', async () => {
  mocks.post.mockRejectedValue(new Error('offline'));
  render(ProviderPanel, { kind: 'key', provider: provider([entry('keep'), entry('toggle')]) });
  await toggle('toggle');
  expect(saved()).toEqual([entry('keep')]);
 });

 it('settings save retains raw evidence but never persists computed effort authority', async () => {
  const model = { ...entry('saved'), effort_levels: ['max'], default_effort: 'max', context_window: 123, pricing: { prompt: '0.1' } };
  mocks.post.mockResolvedValue({ source: 'upstream', models: [] });
  render(ProviderPanel, { kind: 'key', provider: provider([model]) });
  await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'Renamed' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
  expect(saved()).toEqual([{ ...entry('saved'), context_window: 123, pricing: { prompt: '0.1' } }]);
 });
});
