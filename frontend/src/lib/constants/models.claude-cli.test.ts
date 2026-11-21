import { beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));
import {
	getClaudeAuth,
	getClaudeContextWindow,
	getDefaultModel,
	getModelBadgeClass,
	getModelFamily,
	getModelLabel,
	getModels,
	getNativeModel,
	hydrateModelRegistry,
	isPresetModel,
	normalizeModel,
	resolveDefaultModel,
	setReadyProviders,
	supportsAutoPermission
} from './models.svelte';

const levels = ['low', 'medium', 'high', 'max'];

describe('Claude subscription CLI model list', () => {
	beforeEach(async () => {
		mocks.get.mockResolvedValue({
			default: 'sonnet',
			// The server leaves ids the CLI lists out of the legacy map.
			legacy_map: { 'claude-opus-4-8': 'opus', 'claude-opus-4-6[1m]': 'opus' },
			models: [
				{ value: 'haiku', label: 'Haiku 5.5', family: 'haiku', context_window: 1_000_000 },
				{ value: 'sonnet', label: 'Sonnet 5.5', family: 'sonnet', context_window: 1_000_000 },
				{ value: 'opus', label: 'Opus 5.5', family: 'opus', context_window: 1_000_000 },
				{ value: 'fable', label: 'Fable 5.2', family: 'fable', context_window: 1_000_000 },
				{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus', context_window: 1_000_000, effort_levels: levels },
				{ value: 'claude-mythos-5-1', label: 'Mythos 5.1', family: 'mythos', context_window: 1_000_000, effort_levels: levels },
				{ value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5', family: 'haiku', context_window: 200_000, effort_levels: [] }
			],
			providers: [],
			claude: { auth_kind: 'subscription', plan_type: 'max', cli_models: { cli_version: '2.1.325', captured_at: '2026-11-14T00:00:00Z' } }
		});
		await hydrateModelRegistry();
	});

	it('labels aliases as the CLI names them and offers every listed version', () => {
		expect(getModels().map((m) => m.label)).toEqual(
			['Haiku 5.5', 'Sonnet 5.5', 'Opus 5.5', 'Fable 5.2', 'Opus 4.6', 'Mythos 5.1', 'Haiku 4.5']
		);
		expect(getClaudeAuth().cli_models?.cli_version).toBe('2.1.325');
	});

	it('keeps a listed id as itself and describes it as that model', () => {
		expect(normalizeModel('claude-opus-4-6')).toBe('claude-opus-4-6');
		expect(isPresetModel('claude-opus-4-6')).toBe(true);
		expect(getNativeModel('claude-opus-4-6')?.effort_levels).toEqual(levels);
		// Unlisted legacy ids still fold onto today's alias.
		expect(normalizeModel('claude-opus-4-8')).toBe('opus');
	});

	it('describes a listed version by its own family, window and effort', () => {
		expect(getModelFamily('claude-mythos-5-1')).toBe('mythos');
		expect(getModelBadgeClass('claude-mythos-5-1')).toBe('card-badge--mythos');
		expect(isPresetModel('claude-mythos-5-1')).toBe(true);
		expect(getClaudeContextWindow('claude-haiku-4-5-20251001')).toBe(200_000);
		expect(getNativeModel('claude-haiku-4-5-20251001')?.label).toBe('Haiku 4.5');
	});

	it('keeps live metadata for hidden subscription models while omitting them from getModels()', async () => {
		mocks.get.mockResolvedValueOnce({
			default: 'haiku',
			legacy_map: {},
			models: [
				{ value: 'haiku', label: 'Haiku 5.5', family: 'haiku', context_window: 1_000_000, auto_permission_supported: true }
			],
			hidden_models: [
				{ value: 'opus', label: 'Opus 5.5', family: 'opus', context_window: 1_000_000, effort_levels: levels, auto_permission_supported: true },
				{ value: 'claude-mythos-5-1', label: 'Mythos 5.1', family: 'mythos', context_window: 1_000_000, effort_levels: levels, auto_permission_supported: true },
				{ value: 'claude-zephyr-1', label: 'Zephyr 1', family: null, context_window: 1_000_000, effort_levels: ['low', 'high'], auto_permission_supported: false }
			],
			providers: [],
			claude: { auth_kind: 'subscription', plan_type: 'max', cli_models: { cli_version: '2.1.325', captured_at: '2026-11-14T00:00:00Z' } }
		});
		await hydrateModelRegistry();

		expect(getModels().map((m) => m.value)).toEqual(['haiku']);
		expect(getModelLabel('opus')).toBe('Opus 5.5');
		expect(getModelLabel('claude-mythos-5-1')).toBe('Mythos 5.1');
		expect(getModelFamily('claude-mythos-5-1')).toBe('mythos');
		expect(getClaudeContextWindow('claude-mythos-5-1')).toBe(1_000_000);
		expect(getNativeModel('claude-mythos-5-1')?.effort_levels).toEqual(levels);
		expect(supportsAutoPermission('claude-mythos-5-1')).toBe(true);
		expect(getModelLabel('claude-zephyr-1')).toBe('Zephyr 1');
		expect(getModelFamily('claude-zephyr-1')).toBeNull();
		expect(getNativeModel('claude-zephyr-1')?.effort_levels).toEqual(['low', 'high']);
	});

	it('preserves an explicit empty server default when all models are disabled', async () => {
		mocks.get.mockResolvedValueOnce({
			default: '',
			legacy_map: {},
			models: [],
			hidden_models: [
				{ value: 'sonnet', label: 'Sonnet 5.5', family: 'sonnet', context_window: 1_000_000 }
			],
			providers: [],
			claude: { auth_kind: 'subscription', plan_type: 'max' }
		});
		await hydrateModelRegistry();
		setReadyProviders(['claude']);
		expect(getDefaultModel()).toBe('');
		expect(resolveDefaultModel('', ['claude'], [], [])).toBe('');
		setReadyProviders(null);
	});
});
