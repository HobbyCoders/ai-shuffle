/**
 * `isFastModeCapable` — which picks may run on the fast service tier.
 *
 * Separate file from models.test.ts because the provider cases hydrate the
 * registry (which mutates module-singleton state); vitest isolates module
 * state per test file, so the static-registry assertions there stay clean.
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import { encodeProviderModel, hydrateModelRegistry, isFastModeCapable } from './models';

describe('isFastModeCapable — native Claude models', () => {
	it('accepts the opus family', () => {
		expect(isFastModeCapable('opus')).toBe(true);
			expect(isFastModeCapable('claude-opus-5-5')).toBe(true);
	});

	it('accepts a legacy opus id that normalizes onto the preset', () => {
		expect(isFastModeCapable('claude-opus-4-8[1m]')).toBe(true);
	});

	it('rejects every other family', () => {
		expect(isFastModeCapable('sonnet')).toBe(false);
		expect(isFastModeCapable('haiku')).toBe(false);
			expect(isFastModeCapable('claude-haiku-5-5')).toBe(false);
			expect(isFastModeCapable('claude-sonnet-5-5')).toBe(false);
		expect(isFastModeCapable('fable')).toBe(false);
	});

	it('rejects unset and unknown values', () => {
		expect(isFastModeCapable(null)).toBe(false);
		expect(isFastModeCapable('')).toBe(false);
		expect(isFastModeCapable('some-custom-model')).toBe(false);
	});

	it('rejects a bare gpt-5 id that is not a provider composite', () => {
		// Without the `prov:` envelope there is no provider to route to, so the
		// pick is just a custom id.
		expect(isFastModeCapable('gpt-5.6')).toBe(false);
	});
});

describe('isFastModeCapable — provider composites', () => {
	it('accepts GPT-5 and GPT-6 on OpenAI/Codex rows and rejects other providers', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'oai',
					name: 'OpenAI',
					provider_type: 'openai',
					models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'gpt-5.6'
				},
				{
					id: 'codex',
					name: 'Codex',
					provider_type: 'chatgpt',
					models: [{ id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol' }],
					default_model: 'gpt-5.6-sol'
				},
				{
					id: 'or',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'gpt-5.6'
				},
				{
					id: 'grok',
					name: 'Grok',
					provider_type: 'xai',
					models: [{ id: 'grok-5', label: 'Grok 5' }],
					default_model: 'grok-5'
				}
			]
		});
		await hydrateModelRegistry();

		expect(isFastModeCapable(encodeProviderModel('oai', 'gpt-5.6'))).toBe(true);
		expect(isFastModeCapable(encodeProviderModel('codex', 'gpt-5.6-sol'))).toBe(true);
		for (const provider of ['oai', 'codex']) {
			for (const model of ['gpt-6-astra', 'gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6.1']) {
				expect(isFastModeCapable(encodeProviderModel(provider, model))).toBe(true);
			}
			expect(isFastModeCapable(encodeProviderModel(provider, 'gpt-6.1-sol-custom'))).toBe(false);
			expect(isFastModeCapable(encodeProviderModel(provider, 'gpt-60-sol'))).toBe(false);
			expect(isFastModeCapable(encodeProviderModel(provider, 'gpt-6-astra-custom'))).toBe(false);
		}
		// Right model id, wrong provider kind.
		expect(isFastModeCapable(encodeProviderModel('or', 'gpt-5.6'))).toBe(false);
		expect(isFastModeCapable(encodeProviderModel('or', 'gpt-6-astra'))).toBe(false);
		expect(isFastModeCapable(encodeProviderModel('or', 'gpt-6.1-sol'))).toBe(false);
		expect(isFastModeCapable('gpt-6.1-sol')).toBe(false);
		expect(isFastModeCapable(encodeProviderModel('or', 'openai/gpt-6-astra'))).toBe(false);
		expect(isFastModeCapable('gpt-6-astra')).toBe(false);
		// The provider kind alone does not give other model families a fast tier.
		expect(isFastModeCapable(encodeProviderModel('oai', 'gpt-4.1'))).toBe(false);
		expect(isFastModeCapable(encodeProviderModel('grok', 'grok-5'))).toBe(false);
	});

	it('falls back to the id match alone when the provider row is unknown', async () => {
		// Pre-hydration (or a row disabled after the pick was stored): the toggle
		// must not blink out while `/models` is still in flight.
		expect(isFastModeCapable(encodeProviderModel('not-hydrated-yet', 'gpt-5.6'))).toBe(true);
		expect(isFastModeCapable(encodeProviderModel('not-hydrated-yet', 'gpt-6-astra'))).toBe(true);
		expect(isFastModeCapable(encodeProviderModel('not-hydrated-yet', 'gpt-6.1-sol'))).toBe(true);
		expect(isFastModeCapable(encodeProviderModel('not-hydrated-yet', 'claude-3'))).toBe(false);
	});
});
