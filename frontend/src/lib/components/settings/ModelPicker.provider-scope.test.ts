import { cleanup, fireEvent, render, within } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import {
	NATIVE_PROVIDER_KEY,
	encodeProviderModel,
	hydrateModelRegistry
} from '$lib/constants/models';
import ModelPicker from './ModelPicker.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: false },
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }
		],
		default: 'sonnet',
		legacy_map: {},
		providers: [
			{
				id: 'openai-row',
				name: 'OpenAI',
				provider_type: 'openai',
				models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
				default_model: 'gpt-5.6'
			},
			{
				id: 'openrouter-row',
				name: 'OpenRouter',
				provider_type: 'openrouter',
				models: [{ id: 'qwen/qwen3', label: 'Qwen 3' }],
				default_model: 'qwen/qwen3'
			}
		]
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	localStorage.clear();
});

describe('ModelPicker provider scope', () => {
	it('keeps focus on the trigger when the picker opens', async () => {
		const { getByRole } = render(ModelPicker, {
			props: {
				value: 'sonnet',
				onSelect: vi.fn()
			}
		});

		const trigger = getByRole('button', { name: 'Sonnet' });
		trigger.focus();
		await fireEvent.click(trigger);

		expect(getByRole('textbox', { name: 'Search models' })).not.toBe(document.activeElement);
		expect(document.activeElement).toBe(trigger);
	});

	it('badges API-authenticated provider glyphs in the rail', async () => {
		const { getByRole } = render(ModelPicker, {
			props: {
				value: 'sonnet',
				onSelect: vi.fn()
			}
		});

		await fireEvent.click(getByRole('button', { name: 'Sonnet' }));

		const apiProvider = getByRole('tab', { name: 'OpenAI · API' });
		const nativeProvider = getByRole('tab', { name: 'Claude' });
		expect(apiProvider.querySelector('[data-provider-api-badge]')?.textContent).toBe('API');
		expect(nativeProvider.querySelector('[data-provider-api-badge]')).toBeNull();
	});

	it('shows only the selected provider and emits its composite model value', async () => {
		const onSelect = vi.fn();
		const { getByRole, queryByText } = render(ModelPicker, {
			props: {
				value: '',
				onSelect,
				providerScope: 'openai-row',
				providerModelsOnly: true,
				allowEmpty: true
			}
		});

		await fireEvent.click(getByRole('button', { name: 'Unavailable as subagent' }));
		const list = within(getByRole('listbox', { name: 'Model' }));

		expect(list.getByRole('option', { name: 'Unavailable as subagent' })).toBeTruthy();
		expect(list.getByRole('option', { name: /GPT-5\.6/ })).toBeTruthy();
		expect(queryByText('Qwen 3')).toBeNull();
		expect(queryByText('Sonnet')).toBeNull();
		expect(queryByText('Custom…')).toBeNull();

		await fireEvent.click(list.getByRole('option', { name: /GPT-5\.6/ }));
		expect(onSelect).toHaveBeenCalledWith(encodeProviderModel('openai-row', 'gpt-5.6'));
	});

	it('keeps native Claude choices separate from configured providers', async () => {
		const onSelect = vi.fn();
		const { getByRole, queryByText } = render(ModelPicker, {
			props: {
				value: 'sonnet',
				onSelect,
				providerScope: NATIVE_PROVIDER_KEY,
				allowEmpty: true
			}
		});

		await fireEvent.click(getByRole('button', { name: 'Sonnet' }));
		const list = within(getByRole('listbox', { name: 'Model' }));

		expect(list.getByRole('option', { name: /Haiku/ })).toBeTruthy();
		expect(list.getByRole('option', { name: /Sonnet/ })).toBeTruthy();
		expect(queryByText('GPT-5.6')).toBeNull();
		expect(queryByText('Qwen 3')).toBeNull();
		expect(queryByText('Custom…')).toBeNull();

		await fireEvent.click(list.getByRole('option', { name: /Haiku/ }));
		expect(onSelect).toHaveBeenCalledWith('haiku');
	});
});
