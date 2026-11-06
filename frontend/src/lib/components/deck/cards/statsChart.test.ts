import { describe, expect, it } from 'vitest';
import { formatScale, labelIndexes, spendScale } from './statsChart';

describe('spendScale', () => {
	it('rounds the top up to three even steps', () => {
		expect(spendScale(5.4)).toEqual({ top: 6, ticks: [2, 4, 6] });
		expect(spendScale(3)).toEqual({ top: 3, ticks: [1, 2, 3] });
		expect(spendScale(7)).toEqual({ top: 7.5, ticks: [2.5, 5, 7.5] });
		expect(spendScale(0.12)).toEqual({ top: 0.15, ticks: [0.05, 0.1, 0.15] });
		expect(spendScale(2400).top).toBe(3000);
	});
	it('keeps a scale for an empty range', () => {
		expect(spendScale(0)).toEqual({ top: 3, ticks: [1, 2, 3] });
	});
	it('never sits below the highest bar', () => {
		for (const max of [0.01, 0.9, 1.01, 4.2, 13, 99.9, 1234]) expect(spendScale(max).top).toBeGreaterThanOrEqual(max);
	});
});

describe('formatScale', () => {
	it('shows cents only when the step needs them', () => {
		expect(formatScale(2)).toBe('$2');
		expect(formatScale(2.5)).toBe('$2.50');
		expect(formatScale(0.05)).toBe('$0.05');
		expect(formatScale(1000)).toBe('$1,000');
	});
});

describe('labelIndexes', () => {
	it('labels first, last and evenly between, 3 to 5 in all', () => {
		expect(labelIndexes(31)).toEqual([0, 10, 20, 30]);
		expect(labelIndexes(7)).toEqual([0, 2, 4, 6]);
		expect(labelIndexes(31, 3)).toEqual([0, 15, 30]);
		expect(labelIndexes(91)).toEqual([0, 30, 60, 90]);
		expect(labelIndexes(5)).toEqual([0, 1, 2, 3, 4]);
		expect(labelIndexes(0)).toEqual([]);
	});
	it('always includes both ends', () => {
		for (const n of [6, 7, 8, 14, 30, 31, 90, 91]) {
			const ix = labelIndexes(n);
			expect(ix[0]).toBe(0);
			expect(ix.at(-1)).toBe(n - 1);
			expect(ix.length).toBeGreaterThanOrEqual(3);
			expect(ix.length).toBeLessThanOrEqual(5);
		}
	});
});
