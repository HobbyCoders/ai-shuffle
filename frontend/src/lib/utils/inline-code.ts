/**
 * Inline code (`like this`) for every markdown surface that renders prose:
 * the chat transcript and the Notes preview.
 *
 * A short span must never break across lines into two pills, so it carries
 * `code-short` and the shared `.prose` rule keeps it on one line. A longer
 * span still wraps (it could not fit otherwise) and `box-decoration-break:
 * clone` draws each piece as part of one continuous chip.
 */
import type { Tokens } from 'marked';
import { escapeHtml } from './escape-html';

/** At or under this many characters, inline code stays on one line. */
export const SHORT_INLINE_CODE = 40;

/** Marked `codespan` renderer: `<code>` with `code-short` for a short span. */
export function renderInlineCode({ text }: Pick<Tokens.Codespan, 'text'>): string {
	const short = text.length <= SHORT_INLINE_CODE ? ' class="code-short"' : '';
	return `<code${short}>${escapeHtml(text).replace(/'/g, '&#39;')}</code>`;
}
