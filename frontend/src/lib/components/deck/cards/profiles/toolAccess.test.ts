/**
 * Tests for the profile Tool Access helpers.
 *
 * Regression guard for the "All Tools Allowed doesn't enable AI tools / count
 * shows 0" bug: AI-tool count and the persisted id list are derived purely from
 * the visible dropdown mode, independent of any prior selection state.
 */

import { describe, it, expect } from 'vitest';
import { computeAiToolCount, computeEffectiveAiTools } from './toolAccess';

const ALL_IDS = ['generateImage', 'generateVideo', 'textTo3D', 'webSearch'];

describe('computeAiToolCount', () => {
	it("'all' counts every AI tool regardless of prior selection", () => {
		expect(computeAiToolCount('all', ALL_IDS, [])).toBe(4);
		expect(computeAiToolCount('all', ALL_IDS, ['generateImage'])).toBe(4);
	});

	it("'none' is always zero", () => {
		expect(computeAiToolCount('none', ALL_IDS, ['generateImage'])).toBe(0);
	});

	it("'allow'/'disallow' reflect the individually selected tools", () => {
		expect(computeAiToolCount('allow', ALL_IDS, ['generateImage', 'webSearch'])).toBe(2);
		expect(computeAiToolCount('disallow', ALL_IDS, ['generateVideo'])).toBe(1);
	});

	it("'all' is zero only when no AI tools exist", () => {
		expect(computeAiToolCount('all', [], [])).toBe(0);
	});
});

describe('computeEffectiveAiTools', () => {
	it("'all' persists every AI tool id even when nothing was pre-selected", () => {
		expect(computeEffectiveAiTools('all', ALL_IDS, [])).toEqual(ALL_IDS);
	});

	it("'all' falls back to current selection when the catalog hasn't loaded", () => {
		expect(computeEffectiveAiTools('all', [], ['generateImage'])).toEqual(['generateImage']);
	});

	it("'none' persists nothing", () => {
		expect(computeEffectiveAiTools('none', ALL_IDS, ['generateImage'])).toEqual([]);
	});

	it("'allow'/'disallow' persist exactly the selected tools", () => {
		expect(computeEffectiveAiTools('allow', ALL_IDS, ['generateImage'])).toEqual(['generateImage']);
		expect(computeEffectiveAiTools('disallow', ALL_IDS, ['webSearch'])).toEqual(['webSearch']);
	});

	it('returns copies, not references to the input arrays', () => {
		const enabled = ['generateImage'];
		const result = computeEffectiveAiTools('allow', ALL_IDS, enabled);
		result.push('mutated');
		expect(enabled).toEqual(['generateImage']);
	});
});
