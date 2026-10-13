import { describe, it, expect } from 'vitest';
import {
	getModels,
	getDefaultModel,
	normalizeModel,
	isPresetModel,
	getClaudeContextWindow,
	getModelLabel,
	getModelFamily,
	getModelBadgeClass
} from './models';

describe('model registry presets', () => {
	it('exposes the four SDK shorthand aliases as presets', () => {
		expect(getModels().map((mdl) => mdl.value)).toEqual(['haiku', 'sonnet', 'opus', 'fable']);
	});

	it('defaults to sonnet', () => {
		expect(getDefaultModel()).toBe('sonnet');
	});

	it('exposes no [1m] variants in the dropdown list', () => {
		expect(getModels().every((mdl) => !mdl.value.endsWith('[1m]'))).toBe(true);
	});

	it('carries each model native context window (Claude slugs are 1M)', () => {
		const byValue = Object.fromEntries(getModels().map((mdl) => [mdl.value, mdl]));
		expect(byValue.haiku.context_window).toBe(1_000_000);
			expect(byValue.haiku.effort_levels).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
			expect(byValue.haiku.default_effort).toBe('medium');
			expect(getClaudeContextWindow('claude-haiku-5-5')).toBe(1_000_000);
			expect(getClaudeContextWindow('claude-haiku-4-5')).toBe(200_000);
			expect(getClaudeContextWindow('claude-opus-5-5')).toBe(1_000_000);
			expect(getClaudeContextWindow('claude-sonnet-5-5')).toBe(1_000_000);
		expect(byValue.sonnet.context_window).toBe(1_000_000);
		expect(byValue.opus.context_window).toBe(1_000_000);
		expect(byValue.fable.context_window).toBe(1_000_000);
	});
});

describe('normalizeModel — flipped legacy map + pass-through', () => {
	it('maps legacy full model IDs to the shorthand alias', () => {
		expect(normalizeModel('claude-haiku-5-5')).toBe('haiku');
			expect(normalizeModel('claude-haiku-4-5')).toBe('claude-haiku-4-5');
		expect(normalizeModel('claude-sonnet-4-6')).toBe('sonnet');
		expect(normalizeModel('claude-opus-4-6')).toBe('opus');
		expect(normalizeModel('claude-opus-4-7')).toBe('opus');
		expect(normalizeModel('claude-opus-4-8')).toBe('opus');
	});

	it('strips a stored legacy [1m] suffix down to the base slug', () => {
		// The suffix was a retired opt-in toggle; persisted profiles still hold it.
		expect(normalizeModel('opus[1m]')).toBe('opus');
		expect(normalizeModel('sonnet[1m]')).toBe('sonnet');
		expect(normalizeModel('fable[1m]')).toBe('fable');
		expect(normalizeModel('claude-opus-4-8[1m]')).toBe('opus');
		expect(normalizeModel('claude-sonnet-4-6[1m]')).toBe('sonnet');
		expect(normalizeModel('my-model[1m]')).toBe('my-model');
	});

	it('passes unknown custom strings through unchanged', () => {
		expect(normalizeModel('claude-sonnet-4-5')).toBe('claude-sonnet-4-5');
		expect(normalizeModel('some-future-model')).toBe('some-future-model');
	});

	it('leaves shorthand presets as-is and empties inherit', () => {
		expect(normalizeModel('sonnet')).toBe('sonnet');
		expect(normalizeModel('')).toBe('');
		expect(normalizeModel(null)).toBe('');
	});
});

describe('preset detection and Claude context windows', () => {
	it('treats legacy [1m] ids as presets, not custom', () => {
		expect(isPresetModel('opus')).toBe(true);
		expect(isPresetModel('opus[1m]')).toBe(true);
		expect(isPresetModel('fable')).toBe(true);
		expect(isPresetModel('claude-opus-4-8[1m]')).toBe(true);
		expect(isPresetModel('totally-custom')).toBe(false);
		expect(isPresetModel('')).toBe(false);
	});

	it('resolves the registered window, including through legacy ids', () => {
		expect(getClaudeContextWindow('sonnet')).toBe(1_000_000);
		expect(getClaudeContextWindow('opus[1m]')).toBe(1_000_000);
		expect(getClaudeContextWindow('claude-opus-4-8')).toBe(1_000_000);
		expect(getClaudeContextWindow('haiku')).toBe(1_000_000);
		expect(getClaudeContextWindow('totally-custom')).toBe(null);
		expect(getClaudeContextWindow('')).toBe(null);
	});
});

describe('labels, families, and badge classes', () => {
	it('labels and families resolve through legacy ids, with no 1M decoration', () => {
		expect(getModelLabel('sonnet')).toBe('Sonnet');
		expect(getModelLabel('sonnet[1m]')).toBe('Sonnet');
		expect(getModelFamily('fable')).toBe('fable');
		expect(getModelFamily('opus[1m]')).toBe('opus');
	});

	it('maps families to badge classes, including fable', () => {
		expect(getModelBadgeClass('fable')).toBe('card-badge--fable');
		expect(getModelBadgeClass('sonnet')).toBe('card-badge--sonnet');
		expect(getModelBadgeClass('')).toBe('card-badge--inherit');
		expect(getModelBadgeClass('totally-custom')).toBe('card-badge--muted');
	});
});
