/**
 * R7: the docs markdown is no longer eagerly inlined into the bundle. These
 * tests pin the contract the (unchanged) DocsCard relies on: after
 * `preloadAllDocs()` every synchronous accessor behaves exactly as it did when
 * the glob was eager.
 */
import { describe, expect, it } from 'vitest';
import {
	categories,
	docs,
	docsLoaded,
	getAllSlugs,
	getDocByFullSlug,
	getDocBySlug,
	getDocsByCategory,
	loadDoc,
	preloadAllDocs
} from './index';

describe('docs registry', () => {
	it('does not build the registry at module init', () => {
		// Import side effects must not pull the markdown chunk in.
		// (This runs before any preload in this file.)
		expect(docsLoaded()).toBe(false);
	});

	it('builds the whole registry on preload, in place', async () => {
		const identity = docs;
		await preloadAllDocs();

		expect(docs).toBe(identity); // stable reference for captured consumers
		expect(docs.length).toBeGreaterThan(10);
		expect(docsLoaded()).toBe(true);
	});

	it('parses frontmatter into title/description/order and strips it from content', async () => {
		await preloadAllDocs();
		const entry = docs.find((d) => d.category === 'api-reference');
		expect(entry).toBeDefined();
		expect(entry!.title.length).toBeGreaterThan(0);
		expect(entry!.content.startsWith('<!--')).toBe(false);
		expect(Number.isFinite(entry!.order)).toBe(true);
	});

	it('sorts by category order then doc order', async () => {
		await preloadAllDocs();
		const catOrder = Object.fromEntries(categories.map((c) => [c.id, c.order]));
		for (let i = 1; i < docs.length; i++) {
			const prev = catOrder[docs[i - 1].category];
			const cur = catOrder[docs[i].category];
			expect(prev).toBeLessThanOrEqual(cur);
			if (prev === cur) expect(docs[i - 1].order).toBeLessThanOrEqual(docs[i].order);
		}
	});

	it('keeps the synchronous lookups working', async () => {
		await preloadAllDocs();
		const first = docs[0];
		expect(getDocBySlug(first.category, first.slug)).toBe(first);
		expect(getDocByFullSlug(`${first.category}/${first.slug}`)).toBe(first);
		expect(getDocsByCategory(first.category)).toContain(first);
		expect(getAllSlugs()).toContain(`${first.category}/${first.slug}`);
		expect(getDocBySlug('nope', 'nope')).toBeUndefined();
	});

	it('loadDoc resolves a single document without duplicating it', async () => {
		await preloadAllDocs();
		const before = docs.length;
		const first = docs[0];
		const again = await loadDoc(`${first.category}/${first.slug}`);
		expect(again).toBe(first);
		expect(docs.length).toBe(before);
		expect(await loadDoc('made-up/thing')).toBeUndefined();
	});
});
