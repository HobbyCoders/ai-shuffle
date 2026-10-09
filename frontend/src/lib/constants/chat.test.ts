/**
 * `getContextMaxForModel` — registry-aware context denominator (Chunk U6).
 *
 * Separate file so the `$lib/api/client` mock and the resulting `_registry`
 * mutation (hydration replaces module-singleton state) stay isolated; vitest
 * isolates module state per test file. Mirrors the mocking pattern in
 * models.providers.test.ts.
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import { CONTEXT_MAX, CONTEXT_MAX_1M, getContextMaxForModel } from './chat';
import { hydrateModelRegistry } from './models';

describe('getContextMaxForModel — before hydration (static registry)', () => {
	it('returns the default 200k for an unknown model', () => {
		expect(getContextMaxForModel('openai/gpt-x')).toBe(CONTEXT_MAX);
		expect(CONTEXT_MAX).toBe(200_000);
	});

	it('uses the static Claude windows (sonnet/opus/fable are natively 1M)', () => {
		expect(getContextMaxForModel('sonnet')).toBe(CONTEXT_MAX_1M);
		expect(getContextMaxForModel('opus')).toBe(CONTEXT_MAX_1M);
		expect(getContextMaxForModel('haiku')).toBe(CONTEXT_MAX_1M);
		expect(CONTEXT_MAX_1M).toBe(1_000_000);
	});

	it('resolves a stored legacy [1m] id onto its base slug', () => {
		expect(getContextMaxForModel('opus[1m]')).toBe(CONTEXT_MAX_1M);
		expect(getContextMaxForModel('claude-opus-4-8[1m]')).toBe(CONTEXT_MAX_1M);
	});

	it('no longer guesses 1M from an id that merely contains "1m"', () => {
		// The /1m/i heuristic is gone — the registry is the only source now.
		expect(getContextMaxForModel('claude-sonnet-4-1m')).toBe(CONTEXT_MAX);
		expect(getContextMaxForModel('some-other-1m-model')).toBe(CONTEXT_MAX);
	});

	it('returns the default 200k for a null/undefined model', () => {
		expect(getContextMaxForModel(null)).toBe(CONTEXT_MAX);
		expect(getContextMaxForModel(undefined)).toBe(CONTEXT_MAX);
	});
});

describe('getContextMaxForModel — after hydration (registry-aware)', () => {
	it('honors per-model provider windows and the hydrated Claude windows', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [
				{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 },
				{ value: 'haiku', label: 'Haiku', family: 'haiku', context_window: 1_000_000 }
			],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [
						{ id: 'openai/gpt-x', label: 'GPT-X', context_window: 400000 },
						// Same row, DIFFERENT per-model window — the whole point of the
						// per-model resolution (the row column used to flatten these).
						{ id: 'vendor/big', label: 'Big', context_window: 1_000_000 },
						{ id: 'vendor/plain', label: 'Plain' }
					],
					default_model: 'openai/gpt-x'
				}
			]
		});

		await hydrateModelRegistry();

		// Registry hit, provider scoped.
		expect(getContextMaxForModel('openai/gpt-x', 'prov-1')).toBe(400000);

		// Registry hit, null providerId still resolves by model id across providers.
		expect(getContextMaxForModel('openai/gpt-x')).toBe(400000);

		// Two models on one provider row can now disagree.
		expect(getContextMaxForModel('vendor/big', 'prov-1')).toBe(CONTEXT_MAX_1M);

		// A provider model with no declared window falls back to the default.
		expect(getContextMaxForModel('vendor/plain', 'prov-1')).toBe(CONTEXT_MAX);

		// Claude slugs come from the hydrated Claude side of the registry.
		expect(getContextMaxForModel('sonnet')).toBe(CONTEXT_MAX_1M);
		expect(getContextMaxForModel('haiku')).toBe(CONTEXT_MAX_1M);

		// Unknown-to-registry id → default 200k (no "1m" guessing).
		expect(getContextMaxForModel('some-other-1m-model')).toBe(CONTEXT_MAX);
		expect(getContextMaxForModel('unknown-model')).toBe(CONTEXT_MAX);
	});
});
