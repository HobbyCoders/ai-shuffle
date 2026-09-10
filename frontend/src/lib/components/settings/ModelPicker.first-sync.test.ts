import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
import { encodeProviderModel, getProviders, hydrateModelRegistry, isRegistryHydrated } from '$lib/constants/models';
import { buildModelPickerCatalog } from '$lib/constants/model-picker';
import ModelPicker from './ModelPicker.svelte';

const empty = { models: [], providers: [], default: '', legacy_map: {}, claude: { auth_kind: null, plan_type: null } };
const synced = {
 ...empty,
 // Bundled native metadata does not establish a usable Claude credential.
 models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet' }],
 providers: [{ id: 'synced-provider', name: 'My gateway', provider_type: 'openai', default_model: 'synced/chat',
  models: [{ id: 'synced/chat', label: 'My saved model', effort_levels: [], effort_metadata_version: 1 }] }]
};
beforeEach(async () => { vi.clearAllMocks(); api.get.mockResolvedValue(empty); await hydrateModelRegistry(); });
afterEach(() => { cleanup(); localStorage.clear(); });

it('accepts an empty native catalog without inventing a Claude provider', () => {
 expect(isRegistryHydrated()).toBe(true);
 expect(buildModelPickerCatalog().groups).toEqual([]);
 expect(buildModelPickerCatalog().options).toEqual([]);
});

it('populates the open picker from first sync without visiting Settings or reloading providers', async () => {
 const onSelect = vi.fn();
 render(ModelPicker, { value: encodeProviderModel('synced-provider', 'synced/chat'), onSelect });
 await fireEvent.click(screen.getByRole('button', { name: 'synced/chat' }));
 expect(screen.queryByRole('tab', { name: /Claude/ })).toBeNull();
 api.get.mockResolvedValue(synced);
 await hydrateModelRegistry();
 await tick();
 expect(screen.getByRole('tab', { name: 'OpenAI · API' })).toBeTruthy();
 expect(screen.queryByRole('tab', { name: /Claude/ })).toBeNull();
 await fireEvent.click(screen.getByRole('option', { name: /My saved model/ }));
 expect(onSelect).toHaveBeenCalledWith(encodeProviderModel('synced-provider', 'synced/chat'));
 expect(api.get.mock.calls.every(([path]) => path === '/models')).toBe(true);
 expect(api.post).not.toHaveBeenCalled();
});

it('accepts provider-only snapshots with no native models', async () => {
 api.get.mockResolvedValue({ ...synced, models: [] });
 expect(await hydrateModelRegistry()).toBe(true);
 expect(buildModelPickerCatalog().options.map(m => m.label)).toEqual(['My saved model']);
});

it('does not let a slow pre-sync response erase the synced provider list', async () => {
 let resolveBoot!: (value: unknown) => void;
 api.get.mockReturnValueOnce(new Promise(resolve => { resolveBoot = resolve; })).mockResolvedValueOnce(synced);
 const boot = hydrateModelRegistry();
 expect(await hydrateModelRegistry()).toBe(true);
 resolveBoot(empty);
 expect(await boot).toBe(false);
 expect(getProviders().map(p => p.id)).toEqual(['synced-provider']);
});

it('offers native models only when the backend reports configured Claude auth', async () => {
 api.get.mockResolvedValue({ ...synced, claude: { auth_kind: 'api', plan_type: null } });
 await hydrateModelRegistry();
 expect(buildModelPickerCatalog().groups.map(g => g.key)).toEqual(['__claude__', 'synced-provider']);
});
