import { describe, expect, it } from 'vitest';
import { openRunKeys, parseRange, rangeId, rangesOver, reconcileRanges, type RunSpan } from './run-fold-ranges';

const rows = ['a', 'p', 'b', 'w', 'c', 'q'];
const position = new Map(rows.map((id, at) => [id, at]));
const span = (key: string, through: string): RunSpan => ({
	key, through, start: position.get(key)!, end: position.get(through)!
});

describe('run fold ranges', () => {
	it('parses ranges, bare ids and ids with the separator in them', () => {
		expect(parseRange('a..w', position)).toEqual({ start: 0, end: 3 });
		expect(parseRange('b', position)).toEqual({ start: 2, end: 2 });
		expect(parseRange('a..gone', position)).toEqual({ start: 0, end: 0 });
		// Either surviving endpoint resolves the range to its row.
		expect(parseRange('x..a', position)).toEqual({ start: 0, end: 0 });
		expect(parseRange('gone..w', position)).toEqual({ start: 3, end: 3 });
		expect(parseRange('x..y', position)).toBeNull();
		expect(parseRange('other', position)).toBeNull();
		const dotted = new Map([['m..1', 0], ['m..2', 1]]);
		expect(parseRange('m..1..m..2', dotted)).toEqual({ start: 0, end: 1 });
	});

	it('opens every run a stored range overlaps', () => {
		const spans = [span('a', 'p'), span('b', 'w'), span('c', 'q')];
		expect(openRunKeys(['a..w'], spans, position)).toEqual(new Set(['a', 'b']));
		expect(openRunKeys(['c'], spans, position)).toEqual(new Set(['c']));
		expect(openRunKeys(['elsewhere..x'], spans, position)).toEqual(new Set());
	});

	it('rewrites stored ranges to one per open run, then writes nothing', () => {
		const spans = [span('a', 'p'), span('b', 'w'), span('c', 'q')];
		// A split: one range over two runs becomes one range per run.
		expect(reconcileRanges(['a..w'], spans, position)).toEqual({ add: ['a..p', 'b..w'], remove: ['a..w'] });
		expect(reconcileRanges(['a..p', 'b..w'], spans, position)).toEqual({ add: [], remove: [] });
		// A merge: two ranges inside one run become its one range.
		const merged = [span('a', 'w'), span('c', 'q')];
		expect(reconcileRanges(['a..p', 'b..w'], merged, position)).toEqual({ add: ['a..w'], remove: ['a..p', 'b..w'] });
		// Ranges of another turn's rows are left alone.
		expect(reconcileRanges(['x..y'], spans, position)).toEqual({ add: [], remove: [] });
	});

	it('closes a run by dropping every range over it', () => {
		expect(rangesOver(['a..w', 'c', 'x..y'], span('b', 'w'), position)).toEqual(['a..w']);
		expect(rangeId(span('c', 'q'))).toBe('c..q');
	});
});
