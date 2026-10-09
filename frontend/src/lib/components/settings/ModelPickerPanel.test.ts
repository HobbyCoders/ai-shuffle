import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ModelPickerPanel from './ModelPickerPanel.svelte';
import { reloadFavoriteModels } from '$lib/stores/modelFavorites.svelte';
import { hydrateModelRegistry } from '$lib/constants/models';
import { buildModelPickerCatalog } from '$lib/constants/model-picker';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

const groups = [
	{ key: '__claude__', name: 'Claude', subtext: null, glyph: 'claude' as const, isApi: false }
];
const options = [
	{
		value: 'sonnet',
		label: 'Sonnet',
		description: 'Balanced speed and depth',
		groupKey: '__claude__',
		group: 'Claude',
		groupSubtext: null
	},
	{
		value: 'opus',
		label: 'Opus',
		description: 'Deep reasoning',
		groupKey: '__claude__',
		group: 'Claude',
		groupSubtext: null
	}
];

beforeEach(async () => {
	localStorage.clear();
	reloadFavoriteModels();
	mocks.get.mockResolvedValue({
		models: [
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 },
			{ value: 'opus', label: 'Opus', family: 'opus', context_window: 1_000_000 },
			{ value: 'claude-haiku-4-5', label: 'Haiku 4.5', family: 'haiku', context_window: 200_000 }
		],
		default: 'sonnet', legacy_map: {},
		providers: [{
			id: 'openai', name: 'OpenAI', provider_type: 'openai', default_model: 'gpt-6-astra',
			models: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra', context_window: 272_000,
				context_window_choices: [256_000, 1_000_000] },
				{ id: 'gpt-6.1-sol', label: 'GPT-6.1 Sol', context_window: 272_000,
					context_window_choices: [272_000, 1_000_000] }]
		}]
	});
	await hydrateModelRegistry();
});
afterEach(() => cleanup());

describe('ModelPickerPanel', () => {
	it('shows selectable 272K and 1M controls for Sol 6.1', async () => {
		const onContextChoiceSelect = vi.fn();
		render(ModelPickerPanel, { props: { ...buildModelPickerCatalog(),
			value: 'prov:openai:gpt-6.1-sol', onSelect: vi.fn(),
			contextChoice: '1m', onContextChoiceSelect } });
		await fireEvent.click(screen.getByRole('radio', { name: '272K · Standard' }));
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('standard');
		await fireEvent.click(screen.getByRole('radio', { name: '1M · Extended' }));
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('1m');
	});
	it('shows Astra context choices and supports keyboard selection without selecting a model', async () => {
		const onContextChoiceSelect = vi.fn();
		const onSelect = vi.fn();
		const { rerender } = render(ModelPickerPanel, {
			props: { ...buildModelPickerCatalog(), value: 'prov:openai:gpt-6-astra', onSelect,
				contextChoice: '1m', onContextChoiceSelect }
		});
		expect(screen.getByRole('option', { name: 'GPT-6 Astra' })).toBeTruthy();
		const standard = screen.getByRole('radio', { name: '256K · Standard' });
		const extended = screen.getByRole('radio', { name: '1M · Extended' });
		expect(extended.tabIndex).toBe(0);
		expect(standard.tabIndex).toBe(-1);
		await fireEvent.keyDown(extended, { key: 'ArrowLeft' });
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('standard');
		expect(document.activeElement).toBe(standard);
		await rerender({ contextChoice: 'standard' });
		expect(standard.getAttribute('aria-checked')).toBe('true');
		expect(standard.tabIndex).toBe(0);
		await fireEvent.keyDown(standard, { key: 'End' });
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('1m');
		expect(document.activeElement).toBe(extended);
		expect(onSelect).not.toHaveBeenCalled();
		await rerender({ value: 'claude-haiku-4-5' });
		expect(screen.queryByRole('radiogroup', { name: 'Context window' })).toBeNull();
	});

	it('keeps Favorites first in the rail and persists functional row stars', async () => {
		render(ModelPickerPanel, {
			props: { groups, options, value: 'sonnet', onSelect: vi.fn() }
		});

		const tabs = screen.getAllByRole('tab');
		expect(tabs[0]?.getAttribute('title')).toBe('Favorites');
		expect(screen.getByRole('tab', { name: /favorites, empty/i })).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: /add opus to favorites/i }));
		expect(JSON.parse(localStorage.getItem('aihub.pinnedModels') ?? '[]')).toEqual(['opus']);

		await fireEvent.click(screen.getByRole('tab', { name: /favorites, 1 models/i }));
		const list = screen.getByRole('listbox', { name: 'Model' });
		expect(within(list).getByRole('option', { name: /opus/i })).toBeTruthy();
		expect(within(list).queryByRole('option', { name: /sonnet/i })).toBeNull();
	});

	it('pins the context selector beneath the list for an eligible selected model', async () => {
		const onContextChoiceSelect = vi.fn();
		render(ModelPickerPanel, {
			props: {
				groups,
				options,
				value: 'sonnet',
				onSelect: vi.fn(),
				contextChoice: '1m',
				onContextChoiceSelect
			}
		});

		expect(screen.getByRole('radiogroup', { name: /context window/i })).toBeTruthy();
		expect(screen.getByRole('radio', { name: /1m.*extended/i }).getAttribute('aria-checked')).toBe(
			'true'
		);
		expect(screen.getByRole('radio', { name: /200k.*standard/i })).toBeTruthy();
		await fireEvent.click(screen.getByRole('radio', { name: /200k.*standard/i }));
		expect(onContextChoiceSelect).toHaveBeenCalledWith('standard');
	});

	it('startOnAll lists every provider even when value belongs to Claude', async () => {
		const grok = {
			key: 'grok-row',
			name: 'Grok',
			subtext: 'API',
			glyph: 'grok' as const,
			isApi: true
		};
		const mixedOptions = [
			...options,
			{
				value: 'prov:grok-row:grok-4',
				label: 'Grok 4',
				description: 'grok-4',
				groupKey: 'grok-row',
				group: 'Grok',
				groupSubtext: 'API'
			}
		];
		render(ModelPickerPanel, {
			props: {
				groups: [...groups, grok],
				options: mixedOptions,
				value: 'sonnet',
				onSelect: vi.fn(),
				startOnAll: true
			}
		});

		expect(screen.getByRole('tab', { name: /all providers/i })).toBeTruthy();
		const list = screen.getByRole('listbox', { name: 'Model' });
		expect(within(list).getByRole('option', { name: /sonnet/i })).toBeTruthy();
		expect(within(list).getByRole('option', { name: /grok 4/i })).toBeTruthy();

		await fireEvent.click(screen.getByRole('tab', { name: /^claude/i }));
		expect(within(list).getByRole('option', { name: /sonnet/i })).toBeTruthy();
		expect(within(list).queryByRole('option', { name: /grok 4/i })).toBeNull();
	});

	it('omits All providers when startOnAll is off, even if value is a Claude model', async () => {
		const grok = {
			key: 'grok-row',
			name: 'Grok',
			subtext: 'API',
			glyph: 'grok' as const,
			isApi: true
		};
		const mixedOptions = [
			...options,
			{
				value: 'prov:grok-row:grok-4',
				label: 'Grok 4',
				description: 'grok-4',
				groupKey: 'grok-row',
				group: 'Grok',
				groupSubtext: 'API'
			}
		];
		render(ModelPickerPanel, {
			props: {
				groups: [...groups, grok],
				options: mixedOptions,
				value: 'sonnet',
				onSelect: vi.fn()
			}
		});

		expect(screen.queryByRole('tab', { name: /all providers/i })).toBeNull();
		const list = screen.getByRole('listbox', { name: 'Model' });
		expect(within(list).getByRole('option', { name: /sonnet/i })).toBeTruthy();
		expect(within(list).queryByRole('option', { name: /grok 4/i })).toBeNull();
	});

	it('labels a ChatGPT or SuperGrok subscription Unofficial, an API key not', () => {
		const chatgpt = { key: 'gpt-row', name: 'OpenAI', subtext: 'PRO', glyph: 'openai' as const, isApi: false, unofficial: true };
		const xai = { key: 'xai-row', name: 'Grok', subtext: 'API', glyph: 'grok' as const, isApi: true };
		const option = (groupKey: string, group: string, groupSubtext: string, id: string) => ({
			value: `prov:${groupKey}:${id}`,
			label: id,
			description: id,
			groupKey,
			group,
			groupSubtext
		});
		render(ModelPickerPanel, {
			props: {
				groups: [chatgpt, xai],
				options: [option('gpt-row', 'OpenAI', 'PRO', 'gpt-5'), option('xai-row', 'Grok', 'API', 'grok-4')],
				value: '',
				onSelect: vi.fn(),
				startOnAll: true
			}
		});
		const list = screen.getByRole('listbox', { name: 'Model' });
		const gpt = within(list).getByRole('option', { name: /gpt-5/ });
		expect(within(gpt).getByTestId('model-unofficial').textContent).toContain('Unofficial');
		expect(gpt.getAttribute('title')).toContain('unofficial sign-in. The provider may change or block it.');
		const grok = within(list).getByRole('option', { name: /grok-4/ });
		expect(within(grok).queryByTestId('model-unofficial')).toBeNull();
		expect(screen.getByRole('tab', { name: 'OpenAI · PRO · Unofficial sign-in' })).toBeTruthy();
	});
});
