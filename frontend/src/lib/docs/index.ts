/**
 * Documentation Registry
 *
 * ## Why the glob is lazy
 *
 * This module used to `import.meta.glob('./content/**\/*.md', { eager: true })`,
 * which inlines **404 kB of raw markdown as string literals** into whatever
 * chunk imports it — and, because MainPanel statically imported DocsCard, that
 * was the primary route bundle. Every user downloaded and parsed the entire
 * manual to render a chat (perf review R7). `buildRegistry()` also ran at module
 * init, so the frontmatter of 35 documents was parsed during boot.
 *
 * Now:
 *  • the glob is non-eager, so the markdown is a separate `docs-content` chunk
 *    (see `manualChunks` in vite.config.js) that only Docs readers fetch;
 *  • the registry is built from that chunk on first load, not at module init;
 *  • `preloadAllDocs()` is awaited by the lazy Docs panel BEFORE DocsCard
 *    mounts, so every existing synchronous consumer (`docs`, `getDocBySlug`,
 *    `DocsSearchEngine`) keeps working unchanged and the UX is identical.
 *
 * The exported `docs` array identity is stable — `preloadAllDocs()` fills it in
 * place, so a module that captured the reference before the load still sees the
 * entries afterwards.
 */

import type { DocEntry, DocCategory } from './types';

// Frontmatter parser: extracts <!-- key: value --> block from top of markdown
function parseFrontmatter(raw: string): { meta: Record<string, string>; content: string } {
	const match = raw.match(/^<!--\s*\n((?:\s*[\w-]+:\s*.+\n)+)\s*-->\s*\n/);
	if (!match) return { meta: {}, content: raw };

	const meta: Record<string, string> = {};
	const fieldRe = /^\s*([\w-]+):\s*(.+?)\s*$/gm;
	let fm;
	while ((fm = fieldRe.exec(match[1])) !== null) {
		meta[fm[1]] = fm[2];
	}

	return { meta, content: raw.slice(match[0].length) };
}

// Category definitions
export const categories: DocCategory[] = [
	{ id: 'getting-started', label: 'Getting Started', icon: 'Rocket', order: 0 },
	{ id: 'connectors', label: 'Connectors', icon: 'Plug', order: 1 },
	{ id: 'features', label: 'Features', icon: 'Sparkles', order: 2 },
	{ id: 'api-reference', label: 'API Reference', icon: 'Code', order: 3 }
];

const categorySet = new Set(categories.map((c) => c.id));

/**
 * Lazy loaders, one per markdown file. Nothing is fetched until `loadDoc*` is
 * called; `import.meta.glob` without `eager` only emits the import map.
 */
const markdownLoaders = import.meta.glob('./content/**/*.md', {
	query: '?raw',
	import: 'default'
}) as Record<string, () => Promise<string>>;

/**
 * The registry. Empty until `preloadAllDocs()` (or `loadDoc()`) has run —
 * callers reach it through the lazy Docs panel, which awaits the preload first.
 */
export const docs: DocEntry[] = [];

const byFullSlug = new Map<string, DocEntry>();

/** `./content/<category>/<slug>.md` → identity, or null when out of scope. */
function parsePath(path: string): { category: string; slug: string } | null {
	const parts = path.replace('./content/', '').replace('.md', '').split('/');
	if (parts.length < 2) return null;
	const category = parts[0];
	if (!categorySet.has(category)) return null;
	return { category, slug: parts[parts.length - 1] };
}

function toEntry(path: string, raw: string): DocEntry | null {
	const id = parsePath(path);
	if (!id) return null;
	const { meta, content } = parseFrontmatter(raw);
	return {
		slug: id.slug,
		title: meta.title || id.slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
		description: meta.description || '',
		category: id.category,
		order: parseInt(meta.order || '99', 10),
		content
	};
}

function sortRegistry() {
	const catOrder = Object.fromEntries(categories.map((c) => [c.id, c.order]));
	docs.sort((a, b) => {
		const catDiff = (catOrder[a.category] ?? 99) - (catOrder[b.category] ?? 99);
		if (catDiff !== 0) return catDiff;
		return a.order - b.order;
	});
}

function register(entry: DocEntry) {
	const key = `${entry.category}/${entry.slug}`;
	if (byFullSlug.has(key)) return;
	byFullSlug.set(key, entry);
	docs.push(entry);
}

let allDocsPromise: Promise<void> | null = null;

/**
 * Load every document and build the registry. Idempotent and deduped; the
 * result is cached for the life of the page.
 *
 * Awaited by the lazy Docs panel before DocsCard mounts, which is what lets the
 * card, its nav and its search index keep reading the registry synchronously.
 */
export function preloadAllDocs(): Promise<void> {
	if (allDocsPromise) return allDocsPromise;
	allDocsPromise = (async () => {
		const paths = Object.keys(markdownLoaders);
		const raws = await Promise.all(paths.map((path) => markdownLoaders[path]()));
		paths.forEach((path, i) => {
			const entry = toEntry(path, raws[i]);
			if (entry) register(entry);
		});
		sortRegistry();
	})().catch((e) => {
		// Let a later attempt retry rather than caching the failure forever.
		allDocsPromise = null;
		throw e;
	});
	return allDocsPromise;
}

/**
 * Load a single document by `category/slug`, without pulling the rest.
 *
 * Additive API: useful once DocsCard can await content per selection (its nav
 * and search still need the full set today).
 */
export async function loadDoc(fullSlug: string): Promise<DocEntry | undefined> {
	const existing = byFullSlug.get(fullSlug);
	if (existing) return existing;
	const path = `./content/${fullSlug}.md`;
	const loader = markdownLoaders[path];
	if (!loader) return undefined;
	const entry = toEntry(path, await loader());
	if (!entry) return undefined;
	register(entry);
	sortRegistry();
	return byFullSlug.get(fullSlug);
}

/** True once the registry holds every document. */
export function docsLoaded(): boolean {
	return docs.length > 0 && docs.length === Object.keys(markdownLoaders).length;
}

export function getDocsByCategory(categoryId: string): DocEntry[] {
	return docs.filter((d) => d.category === categoryId);
}

export function getDocBySlug(categoryId: string, slug: string): DocEntry | undefined {
	return byFullSlug.get(`${categoryId}/${slug}`);
}

export function getDocByFullSlug(fullSlug: string): DocEntry | undefined {
	return byFullSlug.get(fullSlug);
}

export function getAllSlugs(): string[] {
	return docs.map((d) => `${d.category}/${d.slug}`);
}
