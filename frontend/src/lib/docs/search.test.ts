import { describe, expect, it } from 'vitest';
import { docs, preloadAllDocs } from './index';
import { DocsSearchEngine, findMatch } from './search';
import type { DocEntry } from './types';

const doc = (slug: string, title: string, content: string): DocEntry =>
	({ slug, category: 'features', title, description: '', order: 1, content }) as DocEntry;

describe('docs search ranking (#1581)', () => {
	it('ranks the document holding the exact phrase above loose word matches', () => {
		const engine = new DocsSearchEngine([
			// Every word, many times over and in the title, but never together.
			doc('loose', 'Prompt tools', 'Write a prompt. Choose tools. Its tools write prompts. Instructions for tools and prompts.'),
			doc('exact', 'Quick Start', 'Give it a **name**, write its **prompt** (its instructions), and choose its **tools**.')
		]);
		const results = engine.search('write its prompt (its instructions), and choose its tools');
		expect(results.map((r) => r.slug)).toEqual(['features/exact', 'features/loose']);
		expect(results[0].snippets[0]).toContain('write its prompt (its instructions), and choose its tools');
	});

	it('counts the phrase while its last word is still being typed', () => {
		const engine = new DocsSearchEngine([
			doc('loose', 'Remote', 'Remote hosts. Remote workers. Access tokens. Access lists.'),
			doc('exact', 'Other', 'Turn on remote access in Settings.')
		]);
		expect(engine.search('remote acc')[0].slug).toBe('features/exact');
	});

	it('keeps a phrase match whose punctuation differs from the query', () => {
		const engine = new DocsSearchEngine([
			doc('loose', 'Hosts', 'Remote, hosts. Access! permissions.'),
			doc('exact', 'Other', 'Turn on remote access in Settings.')
		]);
		for (const query of ['remote, access!', 'remote-access']) {
			expect(engine.search(query)[0]?.slug, query).toBe('features/exact');
		}
	});

	it('scores a hyphenated word by its separate words, so the page titled with them wins', () => {
		const engine = new DocsSearchEngine([
			doc('endpoints', 'Endpoints', '`GET /api/v1/remote-access/status` and `POST /api/v1/remote-access/enable`.'),
			doc('changelog', 'Changelog', 'Added a remote-access toggle.'),
			doc('remote-access', 'Remote Access', 'Turn on remote access in Settings, then open the link on your phone.'),
			doc('hosts', 'Hosts', 'Remote hosts need access keys.')
		]);
		const slugs = engine.search('remote-access').map((r) => r.slug);
		expect(slugs[0]).toBe('features/remote-access');
		// Every page holding the words together ranks above the one that has them apart.
		expect(slugs.at(-1)).toBe('features/hosts');
	});

	it('puts the snippet and the jump at the phrase, not an earlier lone word', () => {
		const text = `Remote hosts are supported. ${'Padding unrelated context. '.repeat(12)}Enable remote-access here.`;
		const lower = text.toLowerCase();
		expect(findMatch(lower, 'remote access')).toEqual({ at: lower.indexOf('remote-access'), term: 'remote-access' });
		// A whole last word before a half-typed one, and one word as before.
		expect(findMatch('remote accessibility. remote access', 'remote access')).toEqual({ at: 22, term: 'remote access' });
		expect(findMatch(lower, 'remote')).toEqual({ at: 0, term: 'remote' });
		const [result] = new DocsSearchEngine([doc('page', 'Page', text)]).search('remote access');
		expect(result.snippets[0]).toContain('Enable remote-access here');
		expect(result.snippets[0]).not.toContain('Remote hosts');
	});

	it('finds several words that are each too short to index, but not one alone', () => {
		const engine = new DocsSearchEngine([
			doc('faq', 'FAQ', 'What do I need to run it? Where do I get help?'),
			doc('other', 'Other', 'I like it. Do not wait.')
		]);
		const [first, ...rest] = engine.search('do I');
		expect(first?.slug).toBe('features/faq');
		expect(first?.snippets[0]).toContain('What do I need');
		expect(rest).toEqual([]);
		expect(engine.search('do')).toEqual([]);
	});

	it('gives each query word found in the title its own boost', () => {
		const engine = new DocsSearchEngine([doc('a', 'Voice Mode', 'voice mode'), doc('b', 'Voice', 'voice mode')]);
		const [both, one] = engine.search('voice mode');
		expect(both.slug).toBe('features/a');
		// Each title word adds 5 on top of the 3 per exact word.
		expect(both.score - one.score).toBe(5);
	});

	it('finds a sentence pasted from the real docs at the top', async () => {
		await preloadAllDocs();
		const engine = new DocsSearchEngine(docs);
		const [first] = engine.search('write its prompt (its instructions), and choose its tools');
		expect(first.slug).toBe('getting-started/quick-start');
		// Short words together find the FAQ first, as Spotlight's backend does.
		expect(engine.search('do I')[0]?.slug).toBe('getting-started/faq');
		// A hyphenated word finds the page about it first, as Spotlight's backend does.
		for (const query of ['remote-access', 'remote, access!', 'remote access']) {
			expect(engine.search(query)[0]?.slug, query).toBe('getting-started/remote-access');
		}
	});
});
