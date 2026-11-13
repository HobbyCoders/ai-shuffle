import { describe, expect, it } from 'vitest';
import { docs, preloadAllDocs } from './index';
import { DocsSearchEngine } from './search';
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

	it('gives each query word found in the title its own boost', () => {
		const engine = new DocsSearchEngine([doc('a', 'Voice Mode', 'voice mode'), doc('b', 'Voice', 'voice mode')]);
		const [both, one] = engine.search('voice mode');
		expect(both.slug).toBe('features/a');
		// Each title word adds 5 on top of the 3 per exact word.
		expect(both.score - one.score).toBe(5);
	});

	it('finds a sentence pasted from the real docs at the top', async () => {
		await preloadAllDocs();
		const [first] = new DocsSearchEngine(docs).search('write its prompt (its instructions), and choose its tools');
		expect(first.slug).toBe('getting-started/quick-start');
	});
});
