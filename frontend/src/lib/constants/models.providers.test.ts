/**
 * `getProviders()` hydration — separate file from models.test.ts so the
 * `$lib/api/client` mock and the resulting `_registry` mutation (hydration
 * replaces module-singleton state) never leak into the static-registry
 * assertions there; vitest isolates module state per test file.
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import { getModelLabel, getProviders, hydrateModelRegistry, isRegistryHydrated } from './models';

describe('getProviders — hydration from GET /models', () => {
	it('is empty before hydration (no static fallback for providers)', () => {
		expect(getProviders()).toEqual([]);
	});

	it('populates from the registry payload on successful hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		expect(isRegistryHydrated()).toBe(true);
		expect(getProviders()).toEqual([
			{
				id: 'prov-1',
				name: 'OpenRouter',
				provider_type: 'openrouter',
				models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
				default_model: 'openai/gpt-5.6'
			}
		]);
	});

	it('carries per-model context_window through hydration onto getProviders()', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6', context_window: 400000 }],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		expect(getProviders()[0].models[0].context_window).toBe(400000);
	});

	it('falls back to an empty provider list when the payload omits providers', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {}
			// no `providers` key — mirrors an older/degraded backend response.
		});

		await hydrateModelRegistry();

		expect(getProviders()).toEqual([]);
	});
});

describe('getModelLabel — provider registry fallback', () => {
	it('resolves a provider-curated model id to its label after hydration, and falls back to the raw id for an unknown id', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		expect(getModelLabel('openai/gpt-5.6')).toBe('GPT-5.6');
		expect(getModelLabel('openai/unknown-model')).toBe('openai/unknown-model');
	});
});
