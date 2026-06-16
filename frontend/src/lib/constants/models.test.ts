import { describe, it, expect } from 'vitest';
import {
	getModels,
	isPresetModel,
	has1MContext,
	getBaseModel,
	withContextSuffix,
	modelSupports1M,
	getModelLabel,
	getModelFamily,
	CONTEXT_1M_SUFFIX
} from './models';

describe('model registry 1M context handling', () => {
	it('exposes only base models in the dropdown list', () => {
		expect(getModels().every((mdl) => !mdl.value.endsWith(CONTEXT_1M_SUFFIX))).toBe(true);
	});

	it('flags which base models support 1M context', () => {
		expect(modelSupports1M('claude-opus-4-8')).toBe(true);
		expect(modelSupports1M('claude-opus-4-8[1m]')).toBe(true);
		expect(modelSupports1M('claude-haiku-4-5')).toBe(false);
		expect(modelSupports1M('')).toBe(false);
	});

	it('detects the 1M suffix, including via legacy slugs', () => {
		expect(has1MContext('claude-opus-4-8[1m]')).toBe(true);
		expect(has1MContext('claude-opus-4-8')).toBe(false);
		expect(has1MContext('opus[1m]')).toBe(true);
		expect(has1MContext('')).toBe(false);
		expect(has1MContext(null)).toBe(false);
	});

	it('strips the suffix to the base model', () => {
		expect(getBaseModel('claude-opus-4-8[1m]')).toBe('claude-opus-4-8');
		expect(getBaseModel('claude-opus-4-8')).toBe('claude-opus-4-8');
		expect(getBaseModel('opus[1m]')).toBe('claude-opus-4-8');
		expect(getBaseModel('')).toBe('');
	});

	it('toggles the suffix on and off idempotently', () => {
		expect(withContextSuffix('claude-opus-4-8', true)).toBe('claude-opus-4-8[1m]');
		expect(withContextSuffix('claude-opus-4-8[1m]', false)).toBe('claude-opus-4-8');
		expect(withContextSuffix('claude-opus-4-8[1m]', true)).toBe('claude-opus-4-8[1m]');
		expect(withContextSuffix('', true)).toBe('');
		expect(withContextSuffix('my-model', true)).toBe('my-model[1m]');
	});

	it('treats 1M variants as presets, not custom', () => {
		expect(isPresetModel('claude-opus-4-8')).toBe(true);
		expect(isPresetModel('claude-opus-4-8[1m]')).toBe(true);
		expect(isPresetModel('opus[1m]')).toBe(true);
		expect(isPresetModel('totally-custom')).toBe(false);
		expect(isPresetModel('')).toBe(false);
	});

	it('labels and families resolve through the suffix', () => {
		expect(getModelLabel('claude-opus-4-8')).toBe('Opus 4.8');
		expect(getModelLabel('claude-opus-4-8[1m]')).toBe('Opus 4.8 1M');
		expect(getModelFamily('claude-opus-4-8[1m]')).toBe('opus');
	});
});
