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
	hydrateModelRegistry
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

describe('getProviderModels — R7 live-switch scoping', () => {
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
