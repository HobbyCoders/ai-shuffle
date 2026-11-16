import { describe, expect, it } from 'vitest';
import { codePoints, cutCodePoints } from './code-points';

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
