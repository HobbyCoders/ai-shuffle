import { describe, expect, it } from 'vitest';
import { codePoints, cutCodePoints, limitEdit } from './code-points';

const family = '👩‍👩‍👧';

describe('cutCodePoints', () => {
	it('counts code points and never splits a character', () => {
		expect(codePoints('😀')).toBe(1);
		expect(cutCodePoints('ab😀', 2)).toBe('ab');
		expect(cutCodePoints('ab' + family, 6)).toBe('ab');
		expect(cutCodePoints('ab' + family, 7)).toBe('ab' + family);
		expect(cutCodePoints('éé', 3)).toBe('é');
	});
});

describe('limitEdit', () => {
	it('leaves a value within the limit alone', () => {
		expect(limitEdit('abc', 1, 5)).toEqual({ value: 'abc', caret: 1 });
	});
	it('cuts the text before the caret and keeps the text after it', () => {
		// "XYZ" pasted at offset 2 of "abcd" with room for one more character.
		expect(limitEdit('abXYZcd', 5, 5)).toEqual({ value: 'abXcd', caret: 3 });
	});
	it('reports the caret in UTF-16 units after an emoji', () => {
		expect(limitEdit('😀😀😀z', 6, 3)).toEqual({ value: '😀😀z', caret: 4 });
	});
	it('keeps the start when the text after the caret alone is too long', () => {
		expect(limitEdit('Xabcdef', 1, 4)).toEqual({ value: 'Xabc', caret: 1 });
	});
});
