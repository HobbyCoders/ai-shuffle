/**
 * Documentation Registry
 *
 * Loads all markdown files from docs/content/ via Vite glob import,
 * parses frontmatter metadata, and provides lookup/navigation helpers.
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

// Load all markdown files eagerly as raw strings
const markdownModules = import.meta.glob('./content/**/*.md', {
	eager: true,
	query: '?raw',
	import: 'default'
}) as Record<string, string>;

// Parse all docs
function buildRegistry(): DocEntry[] {
	const entries: DocEntry[] = [];

	for (const [path, raw] of Object.entries(markdownModules)) {
		// Path format: ./content/category/slug.md
		const parts = path.replace('./content/', '').replace('.md', '').split('/');
		if (parts.length < 2) continue;

		const category = parts[0];
		if (!categorySet.has(category)) continue;

		const slug = parts[parts.length - 1];
		const { meta, content } = parseFrontmatter(raw);

		entries.push({
			slug,
			title: meta.title || slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
			description: meta.description || '',
			category,
			order: parseInt(meta.order || '99', 10),
			content
		});
	}

	// Sort by category order, then doc order
	const catOrder = Object.fromEntries(categories.map((c) => [c.id, c.order]));
	entries.sort((a, b) => {
		const catDiff = (catOrder[a.category] ?? 99) - (catOrder[b.category] ?? 99);
		if (catDiff !== 0) return catDiff;
		return a.order - b.order;
	});

	return entries;
}

export const docs: DocEntry[] = buildRegistry();

export function getDocsByCategory(categoryId: string): DocEntry[] {
	return docs.filter((d) => d.category === categoryId);
}

export function getDocBySlug(categoryId: string, slug: string): DocEntry | undefined {
	return docs.find((d) => d.category === categoryId && d.slug === slug);
}

export function getDocByFullSlug(fullSlug: string): DocEntry | undefined {
	const [category, slug] = fullSlug.split('/');
	return getDocBySlug(category, slug);
}

export function getAllSlugs(): string[] {
	return docs.map((d) => `${d.category}/${d.slug}`);
}
