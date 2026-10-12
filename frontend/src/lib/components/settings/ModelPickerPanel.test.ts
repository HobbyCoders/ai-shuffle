import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ModelPickerPanel from './ModelPickerPanel.svelte';
import { reloadFavoriteModels } from '$lib/stores/modelFavorites.svelte';
import { hydrateModelRegistry } from '$lib/constants/models';
import { buildModelPickerCatalog } from '$lib/constants/model-picker';

const mocks = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), registry: null as unknown }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, patch: mocks.patch } }));

/** The stored OpenRouter row behind the registry's openrouter provider. */
const openrouterRow = () => ({
	id: 'or', name: 'OpenRouter', provider_type: 'openrouter',
	models: [
		{ id: 'moonshotai/kimi-k3', label: 'Kimi K3', context_window: 262_000,
			pricing: { prompt: '0.000001' }, aliases: ['kimi'], enabled: true,
			supported_parameters: ['reasoning'], top_provider: { context_length: 262_144, moderated: false } },
		{ id: 'mystery/model', label: 'Mystery', enabled: false, aliases: ['mys'],
			extra: { nested: { keep: [1, 2] } } },
		{ id: 'deepseek/v4', label: 'DeepSeek V4', context_window: 1_048_576,
			default_reasoning_level: 'high', custom_flag: 'x' }
	]
});

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
	mocks.patch.mockReset();
	mocks.registry = {
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
					context_window_choices: [272_000, 1_000_000] },
				{ id: 'gpt-7-nova', label: 'GPT-7 Nova', context_window: 2_000_000,
					context_window_choices: [272_000, 2_000_000] }]
		}, {
			id: 'or', name: 'OpenRouter', provider_type: 'openrouter', default_model: 'moonshotai/kimi-k3',
			models: [{ id: 'moonshotai/kimi-k3', label: 'Kimi K3', context_window: 262_000 },
				{ id: 'mystery/model', label: 'Mystery' },
				{ id: 'deepseek/v4', label: 'DeepSeek V4', context_window: 1_048_576 }]
		}]
	};
	mocks.get.mockImplementation(async (path: string) =>
		path === '/model-providers' ? { providers: [openrouterRow()] } : mocks.registry);
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
		await rerender({
			groups,
			options: [...options, { value: 'claude-haiku-4-5', label: 'Haiku 4.5', description: '',
				groupKey: '__claude__', group: 'Claude', groupSubtext: null }],
			value: 'claude-haiku-4-5'
		});
		// One window: a group of one, selected, that never toggles.
		const group = screen.getByRole('radiogroup', { name: 'Context window' });
		const only = within(group).getByRole('radio');
		expect(only.getAttribute('aria-label')).toBe('200K · Model limit');
		expect(only.getAttribute('aria-checked')).toBe('true');
		expect(only.tabIndex).toBe(0);
		onContextChoiceSelect.mockClear();
		await fireEvent.keyDown(only, { key: 'ArrowRight' });
		await fireEvent.click(only);
		expect(onContextChoiceSelect).not.toHaveBeenCalled();
		expect(screen.getByText(/compacts near/i).textContent).toContain('~200K');
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

	it('shows a ChatGPT or SuperGrok subscription without an Unofficial label', () => {
		const chatgpt = { key: 'gpt-row', name: 'OpenAI', subtext: 'PRO', glyph: 'openai' as const, isApi: false };
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
		expect(gpt.textContent).not.toContain('Unofficial');
		expect(gpt.getAttribute('title')).toBe('gpt-5');
		const grok = within(list).getByRole('option', { name: /grok-4/ });
		expect(grok.textContent).not.toContain('Unofficial');
		expect(screen.getByRole('tab', { name: 'OpenAI · PRO' })).toBeTruthy();
	});
});

describe('ModelPickerPanel Standard cap editor', () => {
	const settle = () => new Promise((resolve) => setTimeout(resolve, 350));

	function renderCap(props: Record<string, unknown> = {}) {
		const onContextStandardChange = vi.fn();
		const onContextChoiceSelect = vi.fn();
		const view = render(ModelPickerPanel, {
			props: {
				groups, options, value: 'sonnet', onSelect: vi.fn(),
				contextChoice: 'standard', onContextChoiceSelect,
				contextStandardTokens: 200_000, onContextStandardChange,
				...props
			}
		});
		return { ...view, onContextStandardChange, onContextChoiceSelect };
	}

	async function openEditor() {
		await fireEvent.click(screen.getByRole('button', { name: 'Adjust' }));
		return screen.getByRole('group', { name: 'Standard context cap' });
	}

	it('says where Standard compacts and opens the editor on demand', async () => {
		renderCap();
		expect(screen.getByText(/compacts near/i).textContent).toContain('~200K');
		const adjust = screen.getByRole('button', { name: 'Adjust' });
		expect(adjust.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByRole('group', { name: 'Standard context cap' })).toBeNull();
		const editor = await openEditor();
		expect(screen.getByRole('button', { name: 'Done' }).getAttribute('aria-expanded')).toBe('true');
		const slider = within(editor).getByRole('slider', { name: 'Standard context cap' }) as HTMLInputElement;
		expect([slider.min, slider.max, slider.value]).toEqual(['200000', '990000', '200000']);
		expect(within(editor).getByRole('button', { name: '200K, default' }).getAttribute('aria-pressed')).toBe('true');
	});

	it('shows a raised cap on the Standard segment and in the summary', () => {
		renderCap({ contextStandardTokens: 450_000 });
		const standard = screen.getByRole('radio', { name: '450K · Standard' });
		expect(standard.textContent).toContain('raised');
		const summary = screen.getByText(/compacts near/i);
		expect(summary.textContent).toContain('~450K');
		expect(summary.textContent).toContain('default 200K');
	});

	it('saves a preset', async () => {
		const { onContextStandardChange } = renderCap();
		const editor = await openEditor();
		await fireEvent.click(within(editor).getByRole('button', { name: '600K' }));
		expect(onContextStandardChange).toHaveBeenCalledWith(600_000);
	});

	it('steps the slider on the 10K grid and saves the settled value once', async () => {
		const { onContextStandardChange } = renderCap();
		const slider = within(await openEditor()).getByRole('slider');
		await fireEvent.keyDown(slider, { key: 'ArrowRight' });
		await fireEvent.keyDown(slider, { key: 'ArrowRight' });
		expect(slider.getAttribute('aria-valuetext')).toBe('220K tokens');
		expect(onContextStandardChange).not.toHaveBeenCalled();
		await settle();
		expect(onContextStandardChange).toHaveBeenCalledTimes(1);
		expect(onContextStandardChange).toHaveBeenCalledWith(220_000);
		await fireEvent.keyDown(slider, { key: 'End' });
		await settle();
		expect(onContextStandardChange).toHaveBeenLastCalledWith(990_000);
	});

	it('lands GPT-6.1 steps on the grid from its 272K floor', async () => {
		const { onContextStandardChange } = renderCap({
			...buildModelPickerCatalog(), value: 'prov:openai:gpt-6.1-sol', contextStandardTokens: null
		});
		const slider = within(await openEditor()).getByRole('slider');
		await fireEvent.keyDown(slider, { key: 'PageUp' });
		await settle();
		expect(onContextStandardChange).toHaveBeenCalledWith(300_000);
	});

	it('takes free entry in K tokens and refuses values outside Standard', async () => {
		const { onContextStandardChange } = renderCap();
		const editor = await openEditor();
		const field = within(editor).getByRole('textbox', { name: /custom standard cap/i }) as HTMLInputElement;
		await fireEvent.focus(field);
		await fireEvent.input(field, { target: { value: '1000' } });
		expect(field.getAttribute('aria-invalid')).toBe('true');
		expect(screen.getByRole('alert').textContent).toBe('Up to 990K. For the full 1M, choose Extended.');
		await fireEvent.keyDown(field, { key: 'Enter' });
		expect(onContextStandardChange).not.toHaveBeenCalled();
		await fireEvent.input(field, { target: { value: '150' } });
		expect(screen.getByRole('alert').textContent).toContain("can't go below 200K");
		await fireEvent.input(field, { target: { value: 'lots' } });
		expect(screen.getByRole('alert').textContent).toContain('like 450');
		await fireEvent.input(field, { target: { value: '450' } });
		expect(screen.queryByRole('alert')).toBeNull();
		await fireEvent.keyDown(field, { key: 'Enter' });
		expect(onContextStandardChange).toHaveBeenLastCalledWith(450_000);
		await fireEvent.input(field, { target: { value: '0.5m' } });
		await fireEvent.keyDown(field, { key: 'Enter' });
		expect(onContextStandardChange).toHaveBeenLastCalledWith(500_000);
	});

	it('drops an invalid entry on blur and reverts on Escape', async () => {
		const { onContextStandardChange } = renderCap({ contextStandardTokens: 400_000 });
		const field = within(await openEditor()).getByRole('textbox') as HTMLInputElement;
		await fireEvent.focus(field);
		await fireEvent.input(field, { target: { value: '5000' } });
		await fireEvent.keyDown(field, { key: 'Escape' });
		expect(field.value).toBe('400');
		expect(screen.queryByRole('alert')).toBeNull();
		await fireEvent.input(field, { target: { value: '9999' } });
		await fireEvent.blur(field);
		expect(field.value).toBe('400');
		expect(onContextStandardChange).not.toHaveBeenCalled();
	});

	it('locks Extended to the full window with no editor', async () => {
		const { rerender } = renderCap({ contextChoice: '1m' });
		expect(screen.getByText(/uses the full/i).textContent).toContain('1M');
		expect(screen.queryByRole('button', { name: 'Adjust' })).toBeNull();
		await rerender({ ...buildModelPickerCatalog(), value: 'prov:openai:gpt-7-nova' });
		expect(screen.getByRole('radio', { name: '2M · Extended' })).toBeTruthy();
		expect(screen.getByText(/uses the full/i).textContent).toContain('2M');
	});

	it('leaves Standard fixed on hosts that do not pass a setter', () => {
		renderCap({ onContextStandardChange: undefined });
		expect(screen.getByRole('radio', { name: '200K · Standard' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Adjust' })).toBeNull();
	});
});

describe('ModelPickerPanel single and unknown windows', () => {
	function renderModel(value: string, props: Record<string, unknown> = {}) {
		return render(ModelPickerPanel, {
			props: {
				...buildModelPickerCatalog(), value, onSelect: vi.fn(),
				contextChoice: '1m', onContextChoiceSelect: vi.fn(),
				contextStandardTokens: null, onContextStandardChange: vi.fn(),
				...props
			}
		});
	}

	it('shows a single known window with its compaction line and Adjust', () => {
		renderModel('prov:or:moonshotai/kimi-k3');
		const control = document.querySelector('[data-model-context-control]');
		expect(control?.getAttribute('data-context-kind')).toBe('single');
		const radios = within(screen.getByRole('radiogroup', { name: 'Context window' })).getAllByRole('radio');
		expect(radios).toHaveLength(1);
		expect(radios[0].getAttribute('aria-label')).toBe('262K · Model limit');
		const summary = screen.getByText(/compacts near/i);
		expect(summary.textContent).toContain('~262K');
		expect(summary.textContent).toContain('model limit');
		expect(screen.getByRole('button', { name: 'Adjust' })).toBeTruthy();
	});

	it('saves the window on the provider entry, keeping the other models as stored', async () => {
		mocks.patch.mockImplementation(async () => {
			const registry = mocks.registry as { providers: { id: string; models: { id: string; context_window?: number }[] }[] };
			registry.providers[1].models[0].context_window = 200_000;
			return {};
		});
		renderModel('prov:or:moonshotai/kimi-k3');
		await fireEvent.click(screen.getByRole('button', { name: 'Adjust' }));
		const editor = screen.getByRole('group', { name: 'Model context limit' });
		await fireEvent.click(within(editor).getByRole('button', { name: '200K' }));
		await vi.waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
		// Every stored entry goes back exactly as stored; only the target's window changes.
		const before = openrouterRow().models;
		const sent = (mocks.patch.mock.calls[0][1] as { models: Record<string, unknown>[] }).models;
		expect(mocks.patch.mock.calls[0][0]).toBe('/model-providers/or');
		expect(sent).toHaveLength(before.length);
		expect(sent[1]).toEqual(before[1]);
		expect(sent[2]).toEqual(before[2]);
		expect(sent[0]).toEqual({ ...before[0], context_window: 200_000 });
		await vi.waitFor(() =>
			expect(screen.getByRole('radio', { name: '200K · Model limit' })).toBeTruthy());
	});

	it('reports a failed save and keeps the previous window', async () => {
		mocks.patch.mockRejectedValue(new Error('offline'));
		renderModel('prov:or:moonshotai/kimi-k3');
		await fireEvent.click(screen.getByRole('button', { name: 'Adjust' }));
		const field = within(screen.getByRole('group', { name: 'Model context limit' }))
			.getByRole('textbox', { name: /model context limit/i }) as HTMLInputElement;
		await fireEvent.focus(field);
		// Leaving the field untouched saves nothing.
		await fireEvent.blur(field);
		expect(mocks.patch).not.toHaveBeenCalled();
		await fireEvent.focus(field);
		await fireEvent.input(field, { target: { value: '5' } });
		expect(screen.getByRole('alert').textContent).toBe('At least 8K.');
		await fireEvent.input(field, { target: { value: '128' } });
		await fireEvent.keyDown(field, { key: 'Enter' });
		await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Could not save: offline'));
		expect(screen.getByRole('radio', { name: '262K · Model limit' })).toBeTruthy();
	});

	it('shows Unknown for a model with no advertised window and never guesses one', async () => {
		renderModel('prov:or:mystery/model');
		const control = document.querySelector('[data-model-context-control]');
		expect(control?.getAttribute('data-context-kind')).toBe('unknown');
		const only = screen.getByRole('radio', { name: 'Unknown · Not advertised' });
		expect(only.getAttribute('aria-checked')).toBe('true');
		expect(control?.textContent).not.toContain('200K');
		expect(screen.getByText(/no known limit/i)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Set limit' }));
		const editor = screen.getByRole('group', { name: 'Model context limit' });
		const field = within(editor).getByRole('textbox') as HTMLInputElement;
		expect(field.value).toBe('');
		await fireEvent.focus(field);
		await fireEvent.input(field, { target: { value: '131' } });
		await fireEvent.keyDown(field, { key: 'Enter' });
		await vi.waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
		const body = mocks.patch.mock.calls[0][1] as { models: { id: string; context_window?: number }[] };
		expect(body.models.find((m) => m.id === 'mystery/model')?.context_window).toBe(131_000);
	});

	it('saves an explicit whole-K entry over a window that is not whole K', async () => {
		renderModel('prov:or:deepseek/v4');
		expect(screen.getByRole('radio', { name: '1.05M · Model limit' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Adjust' }));
		const field = within(screen.getByRole('group', { name: 'Model context limit' }))
			.getByRole('textbox') as HTMLInputElement;
		expect(field.value).toBe('1049');
		await fireEvent.focus(field);
		await fireEvent.input(field, { target: { value: '' } });
		await fireEvent.input(field, { target: { value: '1049' } });
		await fireEvent.keyDown(field, { key: 'Enter' });
		await vi.waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
		const sent = (mocks.patch.mock.calls[0][1] as { models: { id: string; context_window?: number }[] }).models;
		expect(sent.find((m) => m.id === 'deepseek/v4')?.context_window).toBe(1_049_000);
	});

	it('keeps quick sizes exact and treats the current one as a no-op', async () => {
		renderModel('prov:or:deepseek/v4');
		await fireEvent.click(screen.getByRole('button', { name: 'Adjust' }));
		const editor = screen.getByRole('group', { name: 'Model context limit' });
		const current = within(editor).getByRole('button', { name: '1.05M' });
		expect(current.getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(current);
		const field = within(editor).getByRole('textbox') as HTMLInputElement;
		await fireEvent.focus(field);
		await fireEvent.blur(field);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(mocks.patch).not.toHaveBeenCalled();
		await fireEvent.click(within(editor).getByRole('button', { name: '1M' }));
		await vi.waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
		const sent = (mocks.patch.mock.calls[0][1] as { models: { id: string; context_window?: number }[] }).models;
		expect(sent.find((m) => m.id === 'deepseek/v4')?.context_window).toBe(1_000_000);
	});

	it('points a known single window to Settings when the host cannot adjust', () => {
		renderModel('prov:or:moonshotai/kimi-k3', { onContextStandardChange: undefined });
		expect(screen.getByRole('radio', { name: '262K · Model limit' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /adjust|set limit/i })).toBeNull();
		const summary = screen.getByText(/compacts near/i);
		expect(summary.textContent).toContain('~262K');
		expect(summary.textContent).toContain('Settings → Providers');
	});

	it('points to Settings when the host cannot adjust and offers no editor', () => {
		renderModel('prov:or:mystery/model', { onContextStandardChange: undefined });
		expect(screen.queryByRole('button', { name: /set limit|adjust/i })).toBeNull();
		expect(screen.getByText(/settings → providers/i)).toBeTruthy();
	});

	it('hides the section on profile pickers that do not host context controls', () => {
		renderModel('prov:or:moonshotai/kimi-k3', { contextChoice: undefined, onContextChoiceSelect: undefined });
		expect(document.querySelector('[data-model-context-control]')).toBeNull();
	});
});
