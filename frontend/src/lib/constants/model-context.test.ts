import { beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { encodeProviderModel, hydrateModelRegistry } from './models';
import {
	contextChoiceFromWindow,
	contextWindowForChoice,
	getModelContextControl
} from './model-context';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 },
			{ value: 'haiku', label: 'Haiku', family: 'haiku', context_window: 1_000_000 }
		],
		default: 'sonnet',
		legacy_map: {},
		providers: [
			{
				id: 'openai-row',
				name: 'OpenAI',
				provider_type: 'openai',
				models: [
					{ id: 'gpt-6-astra', label: 'GPT-6 Astra', context_window: 272_000, context_window_choices: [256_000, 1_000_000] },
					{ id: 'gpt-6.1-sol', label: 'GPT-6.1 Sol', context_window: 272_000, context_window_choices: [272_000, 1_000_000] },
					{ id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol', context_window: 1_000_000 },
					{ id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra', context_window: 256_000 }
				],
				default_model: 'gpt-5.6-sol'
			},
			{
				id: 'router-row',
				name: 'Router',
				provider_type: 'openrouter',
				models: [
					{ id: 'anthropic/claude-future', label: 'Claude Future', context_window: 1_000_000 },
					{ id: 'gpt-6-astra', label: 'GPT-6 Astra', context_window: 1_000_000, context_window_choices: [] },
					{ id: 'vendor/other', label: 'Other', context_window: 1_000_000 }
				],
				default_model: 'anthropic/claude-future'
			}
		]
	});
	await hydrateModelRegistry();
});

describe('model context choices', () => {
	it('offers and serializes registry-provided 272K and 1M choices for Sol 6.1', () => {
		const sol = encodeProviderModel('openai-row', 'gpt-6.1-sol');
		expect(getModelContextControl(sol)?.standardLabel).toBe('272K');
		expect(contextWindowForChoice(sol, 'standard')).toBe(272_000);
		expect(contextWindowForChoice(sol, '1m')).toBe(1_000_000);
		expect(contextChoiceFromWindow(272_000)).toBe('standard');
	});
	it('offers Astra 1M independently of its smaller default, scoped to its provider', () => {
		const astra = encodeProviderModel('openai-row', 'gpt-6-astra');
		expect(getModelContextControl(astra)?.standardLabel).toBe('256K');
		expect(contextWindowForChoice(astra, 'standard')).toBe(256_000);
		expect(contextWindowForChoice(astra, '1m')).toBe(1_000_000);
		expect(contextWindowForChoice('gpt-6-astra', '1m')).toBe(1_000_000);
		expect(getModelContextControl(encodeProviderModel('router-row', 'gpt-6-astra'))).toBeNull();
	});

	it('offers model-appropriate standard labels for 1M Claude and GPT models', () => {
		expect(getModelContextControl('sonnet')?.standardLabel).toBe('200K');
		expect(
			getModelContextControl(encodeProviderModel('openai-row', 'gpt-5.6-sol'))?.standardLabel
		).toBe('256K');
		expect(
			getModelContextControl(encodeProviderModel('router-row', 'anthropic/claude-future'))
				?.standardLabel
		).toBe('200K');
	});

	it('does not offer a 1M choice to models without a 1M registered ceiling or another family', () => {
		expect(getModelContextControl('claude-haiku-4-5')).toBeNull();
		expect(
			getModelContextControl(encodeProviderModel('openai-row', 'gpt-5.6-terra'))
		).toBeNull();
		expect(
			getModelContextControl(encodeProviderModel('router-row', 'vendor/other'))
		).toBeNull();
	});

	it('turns the semantic choice into the numeric request override', () => {
		expect(contextWindowForChoice('sonnet', 'standard')).toBe(200_000);
		expect(
			contextWindowForChoice(encodeProviderModel('openai-row', 'gpt-5.6-sol'), 'standard')
		).toBe(256_000);
		expect(contextWindowForChoice('sonnet', '1m')).toBe(1_000_000);
		expect(contextWindowForChoice('haiku', '1m')).toBe(1_000_000);
			expect(contextWindowForChoice('haiku', 'standard')).toBe(200_000);
			expect(contextWindowForChoice('claude-haiku-4-5', '1m')).toBeNull();
	});

	it('restores semantic choices from persisted numeric session values', () => {
		expect(contextChoiceFromWindow(200_000)).toBe('standard');
		expect(contextChoiceFromWindow(256_000)).toBe('standard');
		expect(contextChoiceFromWindow(1_000_000)).toBe('1m');
		expect(contextChoiceFromWindow(null)).toBeNull();
	});
});
