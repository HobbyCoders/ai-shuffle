import { beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { encodeProviderModel, hydrateModelRegistry } from './models';
import {
	clampStandardCap,
	contextChoiceFromWindow,
	contextWindowForChoice,
	formatContextTokens,
	getModelContextControl,
	standardCapPresets
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
					{ id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra', context_window: 256_000 },
					{ id: 'gpt-7-nova', label: 'GPT-7 Nova', context_window: 2_000_000, context_window_choices: [272_000, 2_000_000] }
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

	it('formats compact K and M labels', () => {
		expect(formatContextTokens(200_000)).toBe('200K');
		expect(formatContextTokens(272_000)).toBe('272K');
		expect(formatContextTokens(990_000)).toBe('990K');
		expect(formatContextTokens(1_000_000)).toBe('1M');
		expect(formatContextTokens(1_500_000)).toBe('1.5M');
	});
});

describe('the Standard cap', () => {
	it('runs from the family default to one step below the full window', () => {
		const sonnet = getModelContextControl('sonnet')!;
		expect(sonnet.standardTokens).toBe(200_000);
		expect(sonnet.standardMaxTokens).toBe(990_000);
		expect(sonnet.extendedTokens).toBe(1_000_000);
		const sol = getModelContextControl(encodeProviderModel('openai-row', 'gpt-6.1-sol'))!;
		expect([sol.standardTokens, sol.standardMaxTokens]).toEqual([272_000, 990_000]);
	});

	it('locks Extended to the model\'s full window, past 1M when the registry says so', () => {
		const nova = encodeProviderModel('openai-row', 'gpt-7-nova');
		const control = getModelContextControl(nova)!;
		expect(control.extendedTokens).toBe(2_000_000);
		expect(control.extendedLabel).toBe('2M');
		expect(control.standardMaxTokens).toBe(1_990_000);
		expect(contextWindowForChoice(nova, '1m')).toBe(2_000_000);
		// The extended pick ignores any Standard cap.
		expect(contextWindowForChoice(nova, '1m', 600_000)).toBe(2_000_000);
	});

	it('clamps a cap into range in whole K tokens', () => {
		const sonnet = getModelContextControl('sonnet')!;
		expect(clampStandardCap(sonnet, 450_400)).toBe(450_000);
		expect(clampStandardCap(sonnet, 100_000)).toBe(200_000);
		expect(clampStandardCap(sonnet, 1_000_000)).toBe(990_000);
		expect(clampStandardCap(sonnet, Number.NaN)).toBe(200_000);
	});

	it('offers the default plus up to three round presets that fit', () => {
		expect(standardCapPresets(getModelContextControl('sonnet')!)).toEqual([200_000, 400_000, 600_000, 800_000]);
		expect(standardCapPresets(getModelContextControl(encodeProviderModel('openai-row', 'gpt-6.1-sol'))!))
			.toEqual([272_000, 400_000, 600_000, 800_000]);
	});

	it('sends the raised cap for Standard, clamped to the model', () => {
		expect(contextWindowForChoice('sonnet', 'standard', 450_000)).toBe(450_000);
		expect(contextWindowForChoice('sonnet', 'standard', 5_000_000)).toBe(990_000);
		expect(contextWindowForChoice('sonnet', 'standard', null)).toBe(200_000);
		expect(contextWindowForChoice(encodeProviderModel('openai-row', 'gpt-6.1-sol'), 'standard', 240_000)).toBe(272_000);
	});

	it('reads a saved window as Extended only at the model\'s full window', () => {
		const nova = encodeProviderModel('openai-row', 'gpt-7-nova');
		expect(contextChoiceFromWindow(450_000, 'sonnet')).toBe('standard');
		expect(contextChoiceFromWindow(1_000_000, 'sonnet')).toBe('1m');
		expect(contextChoiceFromWindow(1_200_000, nova)).toBe('standard');
		expect(contextChoiceFromWindow(2_000_000, nova)).toBe('1m');
		// Without a model the 1M opt-in threshold decides.
		expect(contextChoiceFromWindow(1_200_000)).toBe('1m');
	});
});
