/**
 * Client-side Documentation Search Engine
 *
 * Lightweight inverted index for instant full-text search
 * across all documentation content. No external dependencies.
 */

import type { DocEntry, SearchResult } from './types';

export class DocsSearchEngine {
	private index = new Map<string, Set<string>>(); // token -> full slugs
	private docs = new Map<string, DocEntry>(); // full slug -> entry
	private built = false;

	constructor(private entries: DocEntry[]) {}

	private ensureBuilt(): void {
		if (this.built) return;
		for (const entry of this.entries) {
			const key = `${entry.category}/${entry.slug}`;
			this.docs.set(key, entry);
			this.indexDoc(key, entry);
		}
		this.built = true;
	}

	private indexDoc(key: string, entry: DocEntry): void {
		const text = `${entry.title} ${entry.description} ${entry.content}`;
		const tokens = this.tokenize(text);
		for (const token of tokens) {
			let set = this.index.get(token);
			if (!set) {
				set = new Set();
				this.index.set(token, set);
			}
			set.add(key);
		}
	}

	search(query: string, limit = 15): SearchResult[] {
		this.ensureBuilt();

		if (!query || query.trim().length < 2) return [];

		const queryTokens = this.tokenize(query);
		if (queryTokens.length === 0) return [];

		const scores = new Map<string, number>();

		for (const token of queryTokens) {
			// Exact token match
			const exact = this.index.get(token);
			if (exact) {
				for (const key of exact) {
					scores.set(key, (scores.get(key) || 0) + 3);
				}
			}

			// Prefix match (for partial typing)
			for (const [indexToken, keys] of this.index) {
				if (indexToken.startsWith(token) && indexToken !== token) {
					for (const key of keys) {
						scores.set(key, (scores.get(key) || 0) + 1);
					}
				}
			}
		}

		// Boost title matches
		for (const [key, score] of scores) {
			const entry = this.docs.get(key)!;
			const titleLower = entry.title.toLowerCase();
			for (const token of queryTokens) {
				if (titleLower.includes(token)) {
					scores.set(key, score + 5);
				}
			}
		}

		return [...scores.entries()]
			.sort((a, b) => b[1] - a[1])
			.slice(0, limit)
			.map(([key, score]) => {
				const entry = this.docs.get(key)!;
				return {
					slug: key,
					title: entry.title,
					category: entry.category,
					score,
					snippets: this.extractSnippets(entry, queryTokens)
				};
			});
	}

	private extractSnippets(entry: DocEntry, tokens: string[]): string[] {
		const snippets: string[] = [];
		const contentLower = entry.content.toLowerCase();

		for (const token of tokens) {
			const idx = contentLower.indexOf(token);
			if (idx === -1) continue;

			const start = Math.max(0, idx - 50);
			const end = Math.min(entry.content.length, idx + token.length + 50);
			let snippet = entry.content.slice(start, end).trim();

			// Clean up markdown syntax
			snippet = snippet.replace(/[#*`_\[\]]/g, '');

			if (start > 0) snippet = '...' + snippet;
			if (end < entry.content.length) snippet = snippet + '...';

			snippets.push(snippet);
			if (snippets.length >= 2) break;
		}

		return snippets;
	}

	private tokenize(text: string): string[] {
		return text
			.toLowerCase()
			.replace(/[^\w\s-]/g, ' ')
			.split(/\s+/)
			.filter((t) => t.length > 2);
	}
}
