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
			{ value: 'haiku', label: 'Haiku', family: 'haiku', context_window: 200_000 }
		],
		default: 'sonnet',
		legacy_map: {},
		providers: [
			{
				id: 'openai-row',
				name: 'OpenAI',
				provider_type: 'openai',
				models: [
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
					{ id: 'vendor/other', label: 'Other', context_window: 1_000_000 }
				],
				default_model: 'anthropic/claude-future'
			}
		]
	});
	await hydrateModelRegistry();
});

describe('model context choices', () => {
	it('offers model-appropriate standard labels for 1M Claude and GPT models', () => {
		expect(getModelContextControl('sonnet')?.standardLabel).toBe('200k');
		expect(
			getModelContextControl(encodeProviderModel('openai-row', 'gpt-5.6-sol'))?.standardLabel
		).toBe('256k');
		expect(
			getModelContextControl(encodeProviderModel('router-row', 'anthropic/claude-future'))
				?.standardLabel
		).toBe('200k');
	});

	it('does not offer a 1M choice to models without a 1M registered ceiling or another family', () => {
		expect(getModelContextControl('haiku')).toBeNull();
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
		expect(contextWindowForChoice('haiku', '1m')).toBeNull();
	});

	it('restores semantic choices from persisted numeric session values', () => {
		expect(contextChoiceFromWindow(200_000)).toBe('standard');
		expect(contextChoiceFromWindow(256_000)).toBe('standard');
		expect(contextChoiceFromWindow(1_000_000)).toBe('1m');
		expect(contextChoiceFromWindow(null)).toBeNull();
	});
});
