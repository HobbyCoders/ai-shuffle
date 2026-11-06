import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
import { encodeProviderModel, hydrateModelRegistry } from '$lib/constants/models';
import ModelPicker from './ModelPicker.svelte';

beforeEach(async () => {
 api.get.mockResolvedValue({models: [], providers: [], default: '', legacy_map: {}, claude: {auth_kind: null, plan_type: null}});
 await hydrateModelRegistry();
});
afterEach(cleanup);
it('preserves the existing label for callers that do not opt in', () => {
 render(ModelPicker, { value: encodeProviderModel('removed-provider', 'saved/model'), onSelect: vi.fn() });
 expect(screen.getByRole('button', { name: 'saved/model' })).toBeTruthy();
});
it('uses an optional label only when the saved selection is missing from the catalog', () => {
 render(ModelPicker, { value: encodeProviderModel('removed-provider', 'saved/model'), fallbackLabel: 'Saved model (unavailable)', onSelect: vi.fn() });
 expect(screen.getByRole('button', { name: 'Saved model (unavailable)' })).toBeTruthy();
 expect(screen.queryByText('saved/model')).toBeNull();
});
it('still uses the actual catalog label when that model is available', async () => {
 api.get.mockResolvedValue({models: [{value:'sonnet',label:'Sonnet',family:'sonnet'}], providers: [], default: 'sonnet', legacy_map: {}, claude: {auth_kind: 'subscription', plan_type: null}});
 await hydrateModelRegistry();
 render(ModelPicker, { value:'sonnet', fallbackLabel:'Saved model (unavailable)', onSelect:vi.fn() });
 expect(screen.getByRole('button', { name:'Sonnet' })).toBeTruthy();
});
