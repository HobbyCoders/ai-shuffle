import { describe, expect, it } from 'vitest';
import { asksQuestion, isProtectedProse } from './work-log';

// Original expressions: use only small inputs for equivalence, never the stall cases.
function oldQuestionProse(content: string): string {
	return content.replace(/`[^`]*`/g, '')
		.replace(/\]\([^)\s]*\)/g, ']')
		.replace(/<https?:\/\/[^>\s]*>/g, '')
		.replace(/\bhttps?:\/\/\S*[^\s.,;:!?)\]'\"]/g, '');
}

function oldAsksQuestion(content: string): boolean {
	const prose = oldQuestionProse(content).replace(/(?:\r?\n[ \t]*(?:[-*+]|\d+[.)])[ \t]+[^\n]*)+[ \t\r\n]*$/, '');
	return /\?["'’”)*_\]\s]*$/.test(prose);
}

function oldIsProtectedProse(content: string): boolean {
	const text = content.trim();
	return !!text && (text.split('\n').filter(line => line.trim()).length >= 2 ||
		/^\s{0,3}(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|~~~)/m.test(text) || oldQuestionProse(text).includes('?'));
}

function generator(seed: number) {
	return <T>(items: readonly T[]): T => {
		seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
		return items[seed % items.length];
	};
}

describe('linear question predicate', () => {
	it('matches the old regexes on small random lists of up to eight lines', () => {
		const pick = generator(678);
		for (let i = 0; i < 1000; i++) {
			const prefix = pick(['Which?', 'Done.', '', '- first?', 'Which?\r', 'Which?\n']);
			const lines = Array.from({ length: pick([0, 1, 2, 3, 4, 5, 6, 7, 8]) }, () =>
				pick(['', ' ', '\t', ' \t']) + pick(['-', '*', '+', '1.', '12)', '١.', 'no', '1x', '']) +
				pick(['', ' ', '  ', '\t', ' \t ', '   ', '    ']) + pick(['', 'item', 'item?', '\r', '`?`', '[label?](target)']));
			const content = [prefix, ...lines].join(pick(['\n', '\r\n'])) +
				pick(['', '\n', '\n \t\r\n', '\nclosing prose.', '\nclosing?', '\v', '\u00a0', '\r']);
			expect(asksQuestion(content), content).toBe(oldAsksQuestion(content));
			expect(isProtectedProse(content), content).toBe(oldIsProtectedProse(content));
		}
	});

	it('matches the old regexes on small random code and URL inputs', () => {
		const pick = generator(679);
		const tokens = ['`', '](', ')', '<https://', '>', 'https://', 'http://', 'x', '?', '!', '.', "'", '"', '/', ' ', '\t', '\n', '\ufeff', '\x1c', 'é'];
		for (let i = 0; i < 2000; i++) {
			const content = Array.from({ length: pick(Array.from({ length: 25 }, (_, n) => n)) }, () => pick(tokens)).join('');
			expect(asksQuestion(content), content).toBe(oldAsksQuestion(content));
			expect(isProtectedProse(content), content).toBe(oldIsProtectedProse(content));
		}
	});

	it('completes issue #678 lists well under one second', () => {
		const start = performance.now();
		for (const gap of [2, 3, 4]) {
			for (let count = 22; count <= 200; count++) {
				for (const marker of ['1.', '-']) {
					const content = 'Which?\n' + (marker + ' '.repeat(gap) + 'item\n').repeat(count);
					expect(asksQuestion(content + 'closing prose.')).toBe(false);
					expect(asksQuestion(content)).toBe(true);
				}
			}
		}
		expect(performance.now() - start).toBeLessThan(500);
	}, 1000);

	it('completes other adversarial inputs well under one second', () => {
		const start = performance.now();
		for (const content of [
			']('.repeat(20000) + '?',
			'<https://'.repeat(20000) + '?',
			'https://' + '?'.repeat(200000),
			'`'.repeat(200000) + '?',
			'Which?\n' + '9'.repeat(200000) + 'x item?',
			'Which?\n' + ' '.repeat(200000) + 'not a list?'
		]) {
			expect(asksQuestion(content)).toBe(true);
			expect(isProtectedProse(content)).toBe(true);
		}
		expect(performance.now() - start).toBeLessThan(500);
	}, 1000);

	it.each([
		{ content: 'Which?\r\n\t12)\titem\r\n+ \r\n', expected: true },
		{ content: 'Which?\n-\t\n+  ', expected: true },
		{ content: 'Which?\n- item\n\u00a0', expected: false }
	])('preserves raw, untrimmed suffixes: $content', ({ content, expected }) => {
		expect(asksQuestion(content)).toBe(expected);
		expect(asksQuestion(content)).toBe(oldAsksQuestion(content));
	});

	it('does not truncate long trailing lists or code spans', () => {
		expect(asksQuestion('Which?\n' + '- item\n'.repeat(2000))).toBe(true);
		expect(asksQuestion('`' + 'code\n'.repeat(2000) + '?`')).toBe(false);
	});
});
