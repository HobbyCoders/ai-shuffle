import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ModelPickerPanel from './ModelPickerPanel.svelte';
import { reloadFavoriteModels } from '$lib/stores/modelFavorites.svelte';

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

beforeEach(() => {
	localStorage.clear();
	reloadFavoriteModels();
});
afterEach(() => cleanup());

describe('ModelPickerPanel', () => {
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

		const context = screen.getByRole('combobox', { name: /context window/i }) as HTMLSelectElement;
		expect(context.value).toBe('1m');
		expect(screen.getByRole('option', { name: /200k.*normal/i })).toBeTruthy();
		await fireEvent.change(context, { target: { value: 'standard' } });
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
});
