/**
 * The picker offers an Antigravity row only while this worker can run it: a
 * Google sign-in that is not signed in here would only be refused.
 */
import { describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { buildModelPickerCatalog } from './model-picker';
import { hydrateModelRegistry } from './models.svelte';

const row = (id: string, auth_mode: string, usable: boolean) => ({
	id, name: id, provider_type: 'antigravity', auth_mode, usable,
	auth_kind: auth_mode === 'gemini-api-key' ? 'api' : 'subscription', plan_type: null,
	default_model: null, models: [{ id: 'gemini-3.1-pro', label: 'Gemini 3.1 Pro' }]
});

describe('Antigravity rows in the model picker', () => {
	it('leaves out a sign-in this worker cannot run and keeps the rest', async () => {
		mocks.get.mockResolvedValue({
			models: [], default: 'sonnet', ready_providers: ['antigravity'],
			providers: [row('agy-out', 'oauth-business', false), row('agy-me', 'oauth-personal', true), row('agy-key', 'gemini-api-key', true)]
		});
		await hydrateModelRegistry();
		const groups = buildModelPickerCatalog({ providerModelsOnly: true }).groups.map((g) => g.key);
		expect(groups).toEqual(['agy-me', 'agy-key']);
	});
});
