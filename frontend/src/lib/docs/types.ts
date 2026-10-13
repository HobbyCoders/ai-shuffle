/**
 * Documentation system types
 */

export interface DocEntry {
	slug: string;
	title: string;
	description: string;
	category: string;
	order: number;
	content: string; // raw markdown
}

export interface DocCategory {
	id: string;
	label: string;
	icon: string;
	order: number;
}

export interface DocSection {
	id: string;
	title: string;
	level: number;
}

export interface SearchResult {
	slug: string;
	title: string;
	category: string;
	score: number;
	snippets: string[];
}
