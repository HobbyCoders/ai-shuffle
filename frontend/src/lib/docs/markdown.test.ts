import { describe, expect, it } from 'vitest';
import { createSlugger, extractHeadings, markdownToPlainText } from './markdown';
import { DocsSearchEngine, findMatch, matchTerms } from './search';
import type { DocEntry } from './types';

describe('docs heading anchors (#1475)', () => {
	it('keeps the first anchor and numbers each repeat, as GitHub does', () => {
		const slug = createSlugger();
		expect(['Removed', 'Removed', 'Removed-1', 'Removed'].map(slug)).toEqual(['removed', 'removed-1', 'removed-1-1', 'removed-2']);
	});

	it('lists h2 and h3 with the anchors the renderer gives every heading, listed or not', () => {
		const md = '# Changes\n\n## 1.1\n\n### Removed\n\n#### Removed\n\n## 1.0\n\n### Removed\n\n## Scripts and `curl`';
		expect(extractHeadings(md)).toEqual([
			{ id: '11', title: '1.1', level: 2 },
			{ id: 'removed', title: 'Removed', level: 3 },
			// The h4 between them took `removed-1`.
			{ id: '10', title: '1.0', level: 2 },
			{ id: 'removed-2', title: 'Removed', level: 3 },
			{ id: 'scripts-and-curl', title: 'Scripts and curl', level: 2 }
		]);
	});
});

describe('docs plain text (#1478)', () => {
	it('reads tables, links, anchors, lists and code as the reader sees them', () => {
		const text = markdownToPlainText([
			'# Plans', '',
			'| Plan | Price |', '| --- | --- |', '| **Ultimate** | $30/month |', '',
			'See [AI Shuffle Cloud](/docs/getting-started/cloud#paid-plans) and <span>this</span>.', '',
			'- one `item`', '- two', '',
			'> quoted', '',
			'```sh', 'echo hi', '```'
		].join('\n'));
		expect(text).toBe('Plans Plan Price Ultimate $30/month See AI Shuffle Cloud and this. one item two quoted echo hi');
	});

	it('builds search snippets from that text, around the match', () => {
		const entry: DocEntry = {
			slug: 'cloud', category: 'getting-started', title: 'Cloud', description: '', order: 1,
			content: '| Plan | Workers |\n| --- | --- |\n| [Ultimate](/docs/x#y) | 30 |'
		} as DocEntry;
		const [result] = new DocsSearchEngine([entry]).search('ultimate');
		expect(result.snippets).toEqual(['Plan Workers Ultimate 30']);
		expect(result.snippets[0]).not.toMatch(/[|#[\]()]/);
	});

	it('matches the whole query first, then each word', () => {
		expect(matchTerms('  Away  for all ')).toEqual(['away for all', 'away', 'for', 'all']);
		expect(matchTerms('pro')).toEqual(['pro']);
		expect(matchTerms('x')).toEqual([]);
	});

	it('takes a whole word, else the start of a word, before a match inside one', () => {
		const text = 'your subscription renews. scripts run, and so does your own script.';
		expect(findMatch(text, '<script>')).toEqual({ at: text.lastIndexOf('script'), term: 'script' });
		expect(findMatch('your subscription renews. scripts run', 'script')).toEqual({ at: 26, term: 'script' });
		expect(findMatch('your subscription renews', 'script')).toEqual({ at: 8, term: 'script' });
		expect(findMatch('nothing here', 'script')).toBeNull();
	});

	it('centres the search snippet on that same match', () => {
		const entry = {
			slug: 'overview', category: 'getting-started', title: 'Overview', description: '', order: 1,
			content: `Pay for a subscription. ${'Filler words. '.repeat(10)}Run your own script before a turn.`
		} as DocEntry;
		const [result] = new DocsSearchEngine([entry]).search('<script>');
		expect(result.snippets[0]).toContain('Run your own script');
		expect(result.snippets[0]).not.toContain('subscription');
	});
});
