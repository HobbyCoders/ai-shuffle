/**
 * `isReroute` (composer reroute guard) + `getProviderModels` (live model-switch
 * list scoping) — Chunk 7 / rulings R1 & R7.
 *
 * Kept in its own file: `getProviderModels` needs a hydrated `_registry`, and
 * hydration mutates module-singleton state — vitest isolates module state per
 * file so this never leaks into the static-registry assertions elsewhere.
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import {
	isReroute,
	getProviderModels,
	hydrateModelRegistry,
	encodeProviderModel,
	decodeProviderModel,
	isProviderModel,
	getModelLabel,
	PROVIDER_MODEL_PREFIX
} from './models';

describe('isReroute — reroute guard', () => {
	it('flags a genuine cross-family reroute on a native session', () => {
		// Requested opus, actually running sonnet, native (provider null) → rerouted.
		expect(isReroute('sonnet', 'opus', null)).toBe(true);
	});

	it('does not flag a same-family turn', () => {
		expect(isReroute('sonnet', 'sonnet', null)).toBe(false);
	});

	it('does not flag when either model is unclassifiable', () => {
		expect(isReroute('totally-unknown', 'opus', null)).toBe(false);
		expect(isReroute('opus', 'totally-unknown', null)).toBe(false);
	});

	it('does not flag when either model is null', () => {
		expect(isReroute(null, 'opus', null)).toBe(false);
		expect(isReroute('opus', null, null)).toBe(false);
	});

	it('R1: a provider session is NEVER rerouted, even when families differ', () => {
		// A gateway model id could substring-match a Claude family
		// (`inferModelFamily` uses substrings), which would otherwise
		// false-positive. The non-null provider short-circuits to false.
		expect(isReroute('openai/gpt-5.6', 'opus', 'prov-openrouter')).toBe(false);
		expect(isReroute('sonnet-flavoured-gateway', 'opus', 'prov-1')).toBe(false);
	});
});

describe('provider-model codec — PA13 shared encode/decode', () => {
	it('round-trips a plain provider/model pair', () => {
		const encoded = encodeProviderModel('prov-1', 'gpt-5.6');
		expect(encoded).toBe(`${PROVIDER_MODEL_PREFIX}prov-1:gpt-5.6`);
		expect(decodeProviderModel(encoded)).toEqual({
			providerId: 'prov-1',
			modelId: 'gpt-5.6'
		});
	});

	it('round-trips model ids containing slashes AND colons', () => {
		// OpenRouter slugs carry both; a naive split(':') corrupts them, which is
		// exactly why the codec splits on the FIRST colon after the prefix only.
		const id = 'meta-llama/llama-3.1-8b-instruct:free';
		const decoded = decodeProviderModel(encodeProviderModel('prov-openrouter', id));
		expect(decoded).toEqual({ providerId: 'prov-openrouter', modelId: id });
	});

	it('round-trips a model id that itself starts with the prefix', () => {
		const decoded = decodeProviderModel(encodeProviderModel('p', 'prov:weird:id'));
		expect(decoded).toEqual({ providerId: 'p', modelId: 'prov:weird:id' });
	});

	it('returns null for anything that is not a well-formed composite', () => {
		for (const value of [
			null,
			undefined,
			'',
			'opus',
			'claude-sonnet-4-5',
			'openai/gpt-5.6',
			'prov:',
			'prov:p',
			'prov::model',
			'prov:p:'
		]) {
			expect(decodeProviderModel(value)).toBeNull();
			expect(isProviderModel(value)).toBe(false);
		}
	});

	it('isProviderModel is true only for composites', () => {
		expect(isProviderModel(encodeProviderModel('p', 'm'))).toBe(true);
	});
});

describe('getModelLabel — composite round-trip', () => {
	it('resolves a composite inside its own provider after hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-openrouter',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [
						{ id: 'meta-llama/llama-3.1-8b-instruct:free', label: 'Llama 3.1 (free)' }
					],
					default_model: null
				}
			]
		});
		await hydrateModelRegistry();

		expect(
			getModelLabel(
				encodeProviderModel('prov-openrouter', 'meta-llama/llama-3.1-8b-instruct:free')
			)
		).toBe('Llama 3.1 (free)');
		// Unknown provider / model → the bare model id, never the raw composite.
		expect(getModelLabel(encodeProviderModel('nope', 'some-model'))).toBe('some-model');
		// Non-composites are untouched.
		expect(getModelLabel('sonnet')).toBe('Sonnet');
		expect(getModelLabel(null)).toBe('Default');
	});
});

describe('getProviderModels — per-provider lookup (R7 scoping superseded by PA13)', () => {
	it('returns [] for a null/empty provider id (native session, no hydration)', () => {
		expect(getProviderModels(null)).toEqual([]);
		expect(getProviderModels('')).toEqual([]);
		expect(getProviderModels(undefined)).toEqual([]);
	});

	it('returns only the matching provider’s curated models after hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-openrouter',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [
						{ id: 'openai/gpt-5.6', label: 'GPT-5.6' },
						{ id: 'anthropic/claude-via-gw', label: 'Claude (gateway)' }
					],
					default_model: 'openai/gpt-5.6'
				},
				{
					id: 'prov-custom',
					name: 'Custom',
					provider_type: 'custom',
					models: [{ id: 'x/model', label: 'X Model' }],
					default_model: 'x/model'
				}
			]
		});

		await hydrateModelRegistry();

		expect(getProviderModels('prov-openrouter')).toEqual([
			{ id: 'openai/gpt-5.6', label: 'GPT-5.6' },
			{ id: 'anthropic/claude-via-gw', label: 'Claude (gateway)' }
		]);
		// A different provider gets only ITS models — never the Claude list.
		expect(getProviderModels('prov-custom')).toEqual([{ id: 'x/model', label: 'X Model' }]);
		// Unknown id → empty (no accidental fallback to Claude models).
		expect(getProviderModels('nope')).toEqual([]);
	});
});
