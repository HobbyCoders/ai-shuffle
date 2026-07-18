import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
	getModels,
	getDefaultModel,
	normalizeModel,
	isPresetModel,
	has1MContext,
	getBaseModel,
	withContextSuffix,
	modelSupports1M,
	getModelLabel,
	getModelFamily,
	getModelBadgeClass,
	CONTEXT_1M_SUFFIX
} from './models';

describe('model registry presets', () => {
	it('exposes the four SDK shorthand aliases as presets', () => {
		expect(getModels().map((mdl) => mdl.value)).toEqual(['haiku', 'sonnet', 'opus', 'fable']);
	});

	it('defaults to sonnet', () => {
		expect(getDefaultModel()).toBe('sonnet');
	});

	it('exposes only base models in the dropdown list', () => {
		expect(getModels().every((mdl) => !mdl.value.endsWith(CONTEXT_1M_SUFFIX))).toBe(true);
	});

	it('flags which base models support 1M context', () => {
		expect(modelSupports1M('sonnet')).toBe(true);
		expect(modelSupports1M('opus')).toBe(true);
		expect(modelSupports1M('fable')).toBe(true);
		expect(modelSupports1M('haiku')).toBe(false);
		expect(modelSupports1M('')).toBe(false);
	});
});

describe('normalizeModel — flipped legacy map + pass-through', () => {
	it('maps legacy full model IDs to the shorthand alias', () => {
		expect(normalizeModel('claude-haiku-4-5')).toBe('haiku');
		expect(normalizeModel('claude-sonnet-4-6')).toBe('sonnet');
		expect(normalizeModel('claude-sonnet-4-6[1m]')).toBe('sonnet[1m]');
		expect(normalizeModel('claude-opus-4-6')).toBe('opus');
		expect(normalizeModel('claude-opus-4-7')).toBe('opus');
		expect(normalizeModel('claude-opus-4-8')).toBe('opus');
		expect(normalizeModel('claude-opus-4-8[1m]')).toBe('opus[1m]');
	});

	it('passes unknown custom strings through unchanged', () => {
		expect(normalizeModel('claude-sonnet-4-5')).toBe('claude-sonnet-4-5');
		expect(normalizeModel('some-future-model')).toBe('some-future-model');
	});

	it('leaves shorthand presets as-is and empties inherit', () => {
		expect(normalizeModel('sonnet')).toBe('sonnet');
		expect(normalizeModel('fable[1m]')).toBe('fable[1m]');
		expect(normalizeModel('')).toBe('');
		expect(normalizeModel(null)).toBe('');
	});
});

describe('model registry 1M context handling', () => {
	it('detects the 1M suffix, including via legacy IDs', () => {
		expect(has1MContext('opus[1m]')).toBe(true);
		expect(has1MContext('opus')).toBe(false);
		expect(has1MContext('claude-opus-4-8[1m]')).toBe(true);
		expect(has1MContext('')).toBe(false);
		expect(has1MContext(null)).toBe(false);
	});

	it('strips the suffix to the base model', () => {
		expect(getBaseModel('opus[1m]')).toBe('opus');
		expect(getBaseModel('opus')).toBe('opus');
		expect(getBaseModel('claude-opus-4-8[1m]')).toBe('opus');
		expect(getBaseModel('')).toBe('');
	});

	it('toggles the suffix on and off idempotently', () => {
		expect(withContextSuffix('opus', true)).toBe('opus[1m]');
		expect(withContextSuffix('opus[1m]', false)).toBe('opus');
		expect(withContextSuffix('opus[1m]', true)).toBe('opus[1m]');
		expect(withContextSuffix('', true)).toBe('');
		expect(withContextSuffix('my-model', true)).toBe('my-model[1m]');
	});

	it('treats 1M variants as presets, not custom', () => {
		expect(isPresetModel('opus')).toBe(true);
		expect(isPresetModel('opus[1m]')).toBe(true);
		expect(isPresetModel('fable')).toBe(true);
		expect(isPresetModel('claude-opus-4-8[1m]')).toBe(true);
		expect(isPresetModel('totally-custom')).toBe(false);
		expect(isPresetModel('')).toBe(false);
	});
});

describe('labels, families, and badge classes', () => {
	it('labels and families resolve through the suffix', () => {
		expect(getModelLabel('sonnet')).toBe('Sonnet');
		expect(getModelLabel('sonnet[1m]')).toBe('Sonnet 1M');
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
