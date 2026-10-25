import { describe, expect, it } from 'vitest';
import type { DiffLine } from '$lib/utils/diff-parser';
import {
	EXCERPT_LIMITS, buildExcerpt, composeDiffFeedbackPrompt, describeRanges, describeScope, fenceFor,
	isSelectableLine, orderedRange, rangeChip, selectedRanges,
} from './diff-feedback';

const add = (n: number, content = `added ${n}`): DiffLine => ({ type: 'addition', content, oldLineNumber: null, newLineNumber: n });
const del = (n: number, content = `removed ${n}`): DiffLine => ({ type: 'deletion', content, oldLineNumber: n, newLineNumber: null });
const ctx = (o: number, n: number, content = 'same'): DiffLine => ({ type: 'context', content, oldLineNumber: o, newLineNumber: n });

describe('line ranges', () => {
	it('orders anchor and focus either way', () => {
		expect(orderedRange(7, 3)).toEqual({ start: 3, end: 7 });
		expect(orderedRange(3, 7)).toEqual({ start: 3, end: 7 });
	});
	it('reports old and new spans separately', () => {
		const ranges = selectedRanges([ctx(10, 10), del(11), del(12), add(11), add(12), add(13), ctx(13, 14)]);
		expect(ranges).toEqual({ old: { start: 10, end: 13 }, new: { start: 10, end: 14 } });
		expect(describeRanges(ranges)).toBe('new 10-14, old 10-13');
		expect(rangeChip(ranges)).toBe('L10–14');
	});
	it('names only the side a one-sided selection touches', () => {
		expect(describeRanges(selectedRanges([add(4)]))).toBe('new 4');
		const removed = selectedRanges([del(8), del(9)]);
		expect(describeRanges(removed)).toBe('old 8-9');
		expect(rangeChip(removed)).toBe('L8–9 (old)');
	});
	it('only anchors to real diff lines', () => {
		expect(isSelectableLine({ type: 'header' })).toBe(false);
		expect(isSelectableLine({ type: 'collapse' })).toBe(false);
		expect(isSelectableLine(add(1))).toBe(true);
	});
});

describe('scope description', () => {
	it('names the base ref and short merge base', () => {
		expect(describeScope({ scope: 'base', baseRef: 'origin/main', mergeBase: '0123456789abcdef' }))
			.toBe('changes vs origin/main (merge base 0123456)');
		expect(describeScope({ scope: 'base' })).toBe('changes vs the base branch');
	});
	it('names the working-tree category', () => {
		expect(describeScope({ scope: 'working', category: 'staged' })).toBe('uncommitted changes (staged)');
		expect(describeScope({ scope: 'working', category: 'modified' })).toBe('uncommitted changes (unstaged)');
		expect(describeScope({ scope: 'working', category: 'untracked' })).toBe('uncommitted changes (new file)');
	});
});

describe('excerpt bounds', () => {
	it('keeps unified prefixes', () => {
		expect(buildExcerpt([ctx(1, 1, 'a'), del(2, 'b'), add(2, 'c')]).text).toBe(' a\n-b\n+c');
	});
	it('caps line count and counts what it left out', () => {
		const lines = Array.from({ length: EXCERPT_LIMITS.maxLines + 5 }, (_, i) => add(i + 1));
		const excerpt = buildExcerpt(lines);
		expect(excerpt.text.split('\n')).toHaveLength(EXCERPT_LIMITS.maxLines);
		expect(excerpt.omitted).toBe(5);
	});
	it('caps long lines and total size', () => {
		const long = 'x'.repeat(1000);
		const excerpt = buildExcerpt([add(1, long), add(2, long)], { maxLines: 10, maxChars: 300, maxLineChars: 200 });
		expect(excerpt.text).toBe(`+${'x'.repeat(200)}…`);
		expect(excerpt.omitted).toBe(1);
	});
	it('picks a fence longer than any backtick run in the code', () => {
		expect(fenceFor('plain')).toBe('```');
		expect(fenceFor('const s = ```md```;')).toBe('````');
	});
});

describe('composeDiffFeedbackPrompt', () => {
	it('builds one message with path, ranges, scope, excerpt and comment', () => {
		const prompt = composeDiffFeedbackPrompt({
			file: 'src/app.ts',
			lines: [del(4, 'let a = 1;'), add(4, 'const a = 1;')],
			scope: { scope: 'base', baseRef: 'origin/main', mergeBase: 'abcdef1234567' },
			comment: '  Why const here?  ',
		});
		expect(prompt).toBe([
			'Review comment on `src/app.ts`, lines new 4, old 4, in the changes vs origin/main (merge base abcdef1):',
			'',
			'```diff',
			'-let a = 1;',
			'+const a = 1;',
			'```',
			'',
			'Why const here?',
		].join('\n'));
	});
	it('mentions renames and omitted lines', () => {
		const lines = Array.from({ length: EXCERPT_LIMITS.maxLines + 1 }, (_, i) => add(i + 1));
		const prompt = composeDiffFeedbackPrompt({
			file: 'src/new.ts', originalFile: 'src/old.ts', lines,
			scope: { scope: 'working', category: 'staged' }, comment: 'Split this up',
		});
		expect(prompt.split('\n')[0]).toBe(`Review comment on \`src/new.ts\` (renamed from \`src/old.ts\`), lines new 1-${EXCERPT_LIMITS.maxLines + 1}, in the uncommitted changes (staged):`);
		expect(prompt).toContain('(1 more selected line not shown)');
		expect(prompt.endsWith('Split this up')).toBe(true);
	});
});
