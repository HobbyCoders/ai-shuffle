import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
import { encodeProviderModel, getProviders, hydrateModelRegistry, isRegistryHydrated } from '$lib/constants/models';
import { buildModelPickerCatalog, selectionUsesSubscription } from '$lib/constants/model-picker';
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
 expect(buildModelPickerCatalog().groups.map(g => g.key)).toEqual(['synced-provider', '__claude__']);
});

// Subscription sign-ins are the owner's own: a picker for other people (an API
// user, a connector) lists only API-key providers.
const mixed = {
 ...synced,
 claude: { auth_kind: 'subscription', plan_type: 'max' },
 providers: [
  ...synced.providers.map(p => ({ ...p, auth_kind: 'api' })),
  { id: 'codex', name: 'ChatGPT', provider_type: 'chatgpt', auth_kind: 'subscription', default_model: 'gpt-5',
   models: [{ id: 'gpt-5', label: 'GPT-5', effort_levels: [], effort_metadata_version: 1 }] },
  { id: 'supergrok', name: 'SuperGrok', provider_type: 'grok', auth_kind: 'subscription', default_model: 'grok-4',
   models: [{ id: 'grok-4', label: 'Grok 4', effort_levels: [], effort_metadata_version: 1 }] }
 ]
};

it('apiKeyOnly leaves out every subscription sign-in, Claude included', async () => {
 api.get.mockResolvedValue(mixed);
 await hydrateModelRegistry();
 expect(buildModelPickerCatalog().groups.map(g => g.key)).toEqual(['synced-provider', 'codex', 'supergrok', '__claude__']);
 const catalog = buildModelPickerCatalog({ apiKeyOnly: true });
 expect(catalog.groups.map(g => g.key)).toEqual(['synced-provider']);
 expect(catalog.options.map(o => o.label)).toEqual(['My saved model']);
 api.get.mockResolvedValue({ ...mixed, claude: { auth_kind: 'api', plan_type: null } });
 await hydrateModelRegistry();
 expect(buildModelPickerCatalog({ apiKeyOnly: true }).groups.map(g => g.key)).toEqual(['synced-provider', '__claude__']);
});

it('the apiKeyOnly picker offers no subscription model to choose', async () => {
 api.get.mockResolvedValue(mixed);
 await hydrateModelRegistry();
 render(ModelPicker, { value: '', onSelect: vi.fn(), allowEmpty: true, emptyLabel: 'Agent default', apiKeyOnly: true, startOnAll: true });
 await fireEvent.click(screen.getByRole('button', { name: 'Agent default' }));
 expect(screen.getByRole('option', { name: /My saved model/ })).toBeTruthy();
 expect(screen.queryByRole('option', { name: /GPT-5|Grok 4|Sonnet/ })).toBeNull();
});

it('selectionUsesSubscription matches the server for saved agent models', async () => {
 api.get.mockResolvedValue(mixed);
 await hydrateModelRegistry();
 expect(selectionUsesSubscription(encodeProviderModel('codex', 'gpt-5'))).toBe(true);
 expect(selectionUsesSubscription('grok-4', 'supergrok')).toBe(true);
 expect(selectionUsesSubscription('sonnet')).toBe(true);
 expect(selectionUsesSubscription(encodeProviderModel('synced-provider', 'synced/chat'))).toBe(false);
 expect(selectionUsesSubscription(undefined)).toBe(false);
 expect(selectionUsesSubscription(encodeProviderModel('removed', 'x'))).toBe(false);
 api.get.mockResolvedValue({ ...mixed, claude: { auth_kind: 'api', plan_type: null } });
 await hydrateModelRegistry();
 expect(selectionUsesSubscription('sonnet')).toBe(false);
});
