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

describe('getContextMaxForModel — before hydration (no registry)', () => {
	it('returns the default 200k for an unknown model', () => {
		expect(getContextMaxForModel('openai/gpt-x')).toBe(CONTEXT_MAX);
		expect(CONTEXT_MAX).toBe(200_000);
	});

	it('falls through to the /1m/ heuristic when the registry is empty', () => {
		expect(getContextMaxForModel('claude-sonnet-4-1m')).toBe(CONTEXT_MAX_1M);
		expect(CONTEXT_MAX_1M).toBe(1_000_000);
	});

	it('returns the default 200k for a null/undefined model', () => {
		expect(getContextMaxForModel(null)).toBe(CONTEXT_MAX);
		expect(getContextMaxForModel(undefined)).toBe(CONTEXT_MAX);
	});
});

describe('getContextMaxForModel — after hydration (registry-aware)', () => {
	it('honors the provider-declared context_window over the heuristic', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [
						{ id: 'openai/gpt-x', label: 'GPT-X', context_window: 400000 },
						// id contains "1m" but declares a real window — registry must win.
						{ id: 'vendor/model-1m', label: 'Model 1M', context_window: 400000 }
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

		// Registry checked BEFORE the /1m/ heuristic: a model id containing "1m"
		// with a declared window cannot spoof the 1M denominator.
		expect(getContextMaxForModel('vendor/model-1m', 'prov-1')).toBe(400000);

		// Unknown-to-registry id still falls through to the /1m/ heuristic.
		expect(getContextMaxForModel('some-other-1m-model')).toBe(CONTEXT_MAX_1M);

		// Unknown-to-registry id with no 1m marker → default 200k.
		expect(getContextMaxForModel('unknown-model')).toBe(CONTEXT_MAX);
	});
});
