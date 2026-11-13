/**
 * Markdown helpers shared by the Docs renderer, its "On this page" list and
 * its search, so all three agree on heading anchors and readable text.
 */
import { marked, type Token, type Tokens } from 'marked';
import type { DocSection } from './types';

/** The options the Docs renderer parses with; the lexer here must match it. */
export const DOC_MARKED_OPTIONS = { gfm: true, breaks: true } as const;

/** A heading's base anchor, from its raw Markdown text. */
export function headingSlug(text: string): string {
	return text
		.toLowerCase()
		.replace(/<[^>]*>/g, '')
		.replace(/[^\w\s-]/g, '')
		.replace(/\s+/g, '-')
		.replace(/-+/g, '-')
		.trim();
}

/**
 * Anchors for one document, in order. A repeated heading gets `-1`, `-2`...
 * (as GitHub does), so the first one keeps the anchor links already use and
 * every later one is still reachable (#1475).
 */
export function createSlugger(): (text: string) => string {
	const seen = new Map<string, number>();
	return (text) => {
		const base = headingSlug(text);
		let slug = base;
		let n = seen.get(base) ?? 0;
		while (seen.has(slug)) slug = `${base}-${++n}`;
		seen.set(base, n);
		seen.set(slug, 0);
		return slug;
	};
}

/** Readable text of inline tokens: link text without its target, code without backticks, no HTML. */
export function inlineText(tokens: Token[] | undefined): string {
	let out = '';
	for (const token of tokens ?? []) {
		// A line break separates words, whether `breaks: true` made it or the source wrote `<br>`.
		if (token.type === 'br' || (token.type === 'html' && /^<br\s*\/?>$/i.test(token.raw.trim()))) { out += ' '; continue; }
		if (token.type === 'html') continue;
		const children = (token as { tokens?: Token[] }).tokens;
		out += children ? inlineText(children) : ((token as { text?: string }).text ?? '');
	}
	return out;
}

/** Every heading of a document, with the same anchors the renderer gives them. */
export function extractHeadings(markdown: string, levels: readonly number[] = [2, 3]): DocSection[] {
	const slug = createSlugger();
	const headings: DocSection[] = [];
	marked.walkTokens(marked.lexer(markdown, DOC_MARKED_OPTIONS), (token) => {
		if (token.type !== 'heading') return;
		const heading = token as Tokens.Heading;
		// Every heading takes its anchor, listed or not, exactly as it renders.
		const id = slug(heading.text);
		if (levels.includes(heading.depth)) headings.push({ id, title: inlineText(heading.tokens).trim(), level: heading.depth });
	});
	return headings;
}

function blockText(tokens: Token[]): string[] {
	const parts: string[] = [];
	for (const token of tokens) {
		switch (token.type) {
			case 'space': case 'hr': case 'html': case 'def': break;
			case 'code': parts.push((token as Tokens.Code).text); break;
			case 'table': {
				const table = token as Tokens.Table;
				for (const row of [table.header, ...table.rows]) parts.push(row.map((cell) => inlineText(cell.tokens)).join(' '));
				break;
			}
			case 'list': for (const item of (token as Tokens.List).items) parts.push(...blockText(item.tokens)); break;
			case 'blockquote': parts.push(...blockText((token as Tokens.Blockquote).tokens)); break;
			default: {
				const children = (token as { tokens?: Token[] }).tokens;
				parts.push(children ? inlineText(children) : ((token as { text?: string }).text ?? ''));
			}
		}
	}
	return parts;
}

/** A document as the reader sees it: no table pipes, link targets, anchors or markup (#1478). */
export function markdownToPlainText(markdown: string): string {
	return blockText(marked.lexer(markdown, DOC_MARKED_OPTIONS))
		.map((part) => part.replace(/\s+/g, ' ').trim())
		.filter(Boolean)
		.join(' ');
}
