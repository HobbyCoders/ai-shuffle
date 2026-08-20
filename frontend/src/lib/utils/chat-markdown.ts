/**
 * The chat transcript's markdown pipeline.
 *
 * A dedicated `Marked` instance (not the global singleton) so the code-block
 * renderer here cannot leak into the other surfaces that still call
 * `marked.parse` directly (docs, thinking blocks).
 *
 * Two things make this safe to call on every streaming delta:
 *  1. `renderChatMarkdown(text, { highlight: false })` is used for the growing
 *     TAIL, so a half-typed fence is never tokenised by Shiki.
 *  2. Highlighting itself is memoised in `$lib/utils/shiki` on code + language,
 *     so the settled PREFIX re-renders are cache hits.
 */
import { Marked, type Tokens } from 'marked';
import { highlightToInnerHtml, resolveShikiLang } from './shiki';

/** Parsed fence info string: ```ts title="src/a.ts" / ```ts:src/a.ts / ```ts src/a.ts */
export interface FenceMeta {
	/** Raw language tag as written, lowercased (e.g. "ts"). Empty when absent. */
	lang: string;
	/** File path/name pulled out of the info string, or '' when there is none. */
	filename: string;
}

const TITLE_RE = /\btitle\s*=\s*["']([^"']+)["']/;
const FILENAME_RE = /\bfile(?:name)?\s*=\s*["']([^"']+)["']/;
/** A bare token that looks like a path: has a slash or a dotted extension. */
const PATHY_RE = /^[\w./\\@~-]*(?:[/\\][\w.-]+|\.[A-Za-z0-9]{1,8})$/;

/** Split a fenced-code info string into a language tag and an optional filename. */
export function parseFenceMeta(info: string | undefined | null): FenceMeta {
	const raw = (info ?? '').trim();
	if (!raw) return { lang: '', filename: '' };

	let filename = '';
	let rest = raw;

	const titled = raw.match(TITLE_RE) ?? raw.match(FILENAME_RE);
	if (titled) {
		filename = titled[1];
		rest = raw.replace(titled[0], ' ').trim();
	}

	const parts = rest.split(/\s+/).filter(Boolean);
	let lang = parts[0] ?? '';

	// `lang:path/to/file` form.
	if (!filename && lang.includes(':')) {
		const idx = lang.indexOf(':');
		const candidate = lang.slice(idx + 1);
		if (candidate) filename = candidate;
		lang = lang.slice(0, idx);
	}

	// `lang path/to/file` form.
	if (!filename && parts.length > 1 && PATHY_RE.test(parts[1])) {
		filename = parts[1];
	}

	// A lone pathy token means the fence named a file, not a language.
	if (!filename && parts.length === 1 && PATHY_RE.test(lang) && lang.includes('.')) {
		filename = lang;
		lang = lang.slice(lang.lastIndexOf('.') + 1);
	}

	return { lang: lang.toLowerCase(), filename };
}

function escapeHtml(s: string): string {
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

const FILE_ICON =
	'<svg class="cb-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
	'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/></svg>';

const COPY_ICON =
	'<svg class="cb-copy" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
	'<rect width="13" height="13" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

const CHECK_ICON =
	'<svg class="cb-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
	'<path d="M20 6 9 17l-5-5"/></svg>';

/**
 * Render one fenced block into the Studio Noir code-block chrome.
 * Exported for tests; the renderer below is the only production caller.
 */
export function renderCodeBlock(code: string, info: string | undefined, highlight: boolean): string {
	const { lang, filename } = parseFenceMeta(info);
	const shikiLang = resolveShikiLang(lang);
	const inner = highlight ? highlightToInnerHtml(code, shikiLang) : null;
	const body = inner ?? escapeHtml(code);

	const head =
		'<div class="codehead">' +
		FILE_ICON +
		(filename ? `<span class="fname">${escapeHtml(filename)}</span>` : '<span class="fname"></span>') +
		`<span class="lang">${escapeHtml((lang || 'text').toUpperCase())}</span>` +
		'<button type="button" class="code-copy-btn" aria-label="Copy code">' +
		COPY_ICON +
		CHECK_ICON +
		'</button>' +
		'</div>';

	return (
		`<div class="codeblock" data-language="${escapeHtml(lang || 'text')}">` +
		head +
		`<pre class="cb-pre"><code>${body}</code></pre>` +
		'</div>'
	);
}

// The renderer reads this immediately, inside the synchronous `parse` call the
// setter wraps — there is no await between the two, so no interleaving.
let _highlightEnabled = true;

const chatMarked = new Marked({
	breaks: true,
	gfm: true,
	renderer: {
		code(token: Tokens.Code): string {
			return renderCodeBlock(token.text, token.lang, _highlightEnabled);
		}
	}
});

/**
 * Parse chat markdown to HTML. The caller still sanitises the result.
 *
 * @param highlight - false for the still-streaming tail (a fence that has not
 *   closed yet must not be tokenised, and re-tokenising per token would be the
 *   single most expensive thing in the stream loop).
 */
export function renderChatMarkdown(text: string, highlight: boolean): string {
	_highlightEnabled = highlight;
	try {
		return chatMarked.parse(text, { async: false }) as string;
	} finally {
		_highlightEnabled = true;
	}
}
