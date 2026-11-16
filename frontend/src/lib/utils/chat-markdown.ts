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
import { Marked, Renderer, type Token, type Tokens } from 'marked';
import { highlightToInnerHtml, resolveShikiLang } from './shiki';
import { escapeHtml } from './escape-html';
import { renderInlineCode } from './inline-code';
import { registerTable } from './table-export';
import { commandBlock, registerCommandBlock } from '$lib/terminal/commands';
import { collapseRawBase64, embedKind, mediaHtml } from './media';

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

const COPY_ICON =
	'<svg class="cb-copy" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
	'<rect width="13" height="13" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

const CHECK_ICON =
	'<svg class="cb-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
	'<path d="M20 6 9 17l-5-5"/></svg>';

/**
 * Render one fenced block into the code-block chrome: one frame, the language
 * as plain text.
 * Exported for tests; the renderer below is the only production caller.
 */
export function renderCodeBlock(code: string, info: string | undefined, highlight: boolean): string {
	const { lang, filename } = parseFenceMeta(info);
	// Shell blocks get terminal buttons once settled (`highlight`): the
	// streaming tail re-renders on every delta and its fence is not final.
	const runnable = highlight ? commandBlock(code, info, lang) : null;
	const blockId = runnable ? registerCommandBlock(runnable, (info ?? '').trim()) : '';
	const shikiLang = resolveShikiLang(lang);
	const inner = highlight ? highlightToInnerHtml(code, shikiLang) : null;
	const body = inner ?? escapeHtml(code);

	const head =
		'<div class="codehead">' +
		(filename ? `<span class="fname">${escapeHtml(filename)}</span>` : '<span class="fname"></span>') +
		`<span class="lang">${escapeHtml(lang || 'text')}</span>` +
		'<button type="button" class="code-copy-btn" aria-label="Copy code">' +
		COPY_ICON +
		CHECK_ICON +
		'</button>' +
		'</div>';

	return (
		`<div class="codeblock" data-language="${escapeHtml(lang || 'text')}"` +
		(blockId ? ` data-cb-id="${blockId}"` : '') +
		'>' +
		head +
		`<pre class="cb-pre"><code>${body}</code></pre>` +
		'</div>'
	);
}

// The renderer reads this immediately, inside the synchronous `parse` call the
// setter wraps — there is no await between the two, so no interleaving.
let _highlightEnabled = true;

/**
 * Media options for one render. Media markdown becomes a player inside
 * marked's own image and link renderers, so only what marked reads as an image
 * or a link in prose gets one: never anything in code, with nested brackets,
 * titles and container blocks resolved by the same parse (#1619). `hide` holds
 * the hrefs a turn's gallery already shows (media-gallery `galleryHiddenEmbeds`).
 */
export interface ChatMedia {
	hide?: ReadonlySet<string>;
}
let _media: ChatMedia | null = null;

/** A token that renders as a player, or as nothing because the gallery shows it. */
function isPlacedMedia(token: Token): boolean {
	if (token.type !== 'image' && token.type !== 'link') return false;
	return !!embedKind(token.type, token.href) || (token.type === 'image' && !!_media?.hide?.has(token.href));
}

const chatMarked = new Marked({
	breaks: true,
	gfm: true,
	renderer: {
		image(this: Renderer, token: Tokens.Image): string {
			if (!_media) return Renderer.prototype.image.call(this, token);
			if (_media.hide?.has(token.href)) return '';
			const kind = embedKind('image', token.href);
			return kind ? mediaHtml(kind, token.href, token.text) : Renderer.prototype.image.call(this, token);
		},
		link(this: Renderer, token: Tokens.Link): string {
			const kind = _media && embedKind('link', token.href);
			if (!kind) return Renderer.prototype.link.call(this, token);
			return _media?.hide?.has(token.href) ? '' : mediaHtml(kind, token.href, token.text);
		},
		// A paragraph of players alone gets no <p>: a player is a block, and the
		// browser would close the <p> before it and leave empty paragraphs.
		paragraph(this: Renderer, token: Tokens.Paragraph): string {
			const bare = _media && token.tokens.some(isPlacedMedia) && token.tokens.every(
				(t) => isPlacedMedia(t) || t.type === 'br' || (t.type === 'text' && !t.raw.trim())
			);
			return bare ? this.parser.parseInline(token.tokens) + '\n' : Renderer.prototype.paragraph.call(this, token);
		},
		// Short inline code stays one chip on one line (see inline-code.ts).
		codespan: renderInlineCode,
		code(token: Tokens.Code): string {
			return renderCodeBlock(token.text, token.lang, _highlightEnabled);
		},
		// Every table sits in a wrapper the message view decorates with a Copy
		// menu. Settled tables also record their markdown source so "Copy as
		// Markdown" keeps links and code; the streaming tail skips the registry.
		table(this: Renderer, token: Tokens.Table): string {
			const id = _highlightEnabled
				? registerTable({
						header: token.header.map((cell) => cell.text),
						rows: token.rows.map((row) => row.map((cell) => cell.text)),
						align: token.align
					})
				: '';
			const inner = Renderer.prototype.table.call(this, token);
			return `<div class="md-table"${id ? ` data-table-id="${id}"` : ''}>${inner}</div>\n`;
		}
	}
});

/**
 * Where a streaming reply splits into a prefix and a tail that are parsed
 * apart: the start of the last top-level block marked's lexer finds in the
 * whole of `text`. Parsing the blocks before it and the blocks from it on
 * gives the same markup as parsing `text` at once, so a streaming render shows
 * what the full render would (#1619). The last block (a fence or HTML block
 * such as <pre> or a comment that has not closed, a list a later line
 * continues) is always in the tail.
 *
 * Lexed fresh each time: appended text can turn earlier lines into another
 * block (a setext heading), so no earlier split is trusted. A reply that
 * defines link references is parsed whole (0), since a definition reaches
 * across blocks.
 *
 * Raw HTML (#1627): a block boundary does not say where the HTML parser and
 * the sanitizer close an element. `<style>`, `<textarea>`, `<iframe>`, a
 * comment, `<svg>`, `<template>`, `<table>` foster-parenting and the rest can
 * swallow later blocks once the full text is parsed, while a tail rendered on
 * its own would start fresh and could draw a player; an unclosed `<a>` keeps
 * later URLs as text. So the split only lands where every raw tag before it
 * is a plain inline element, properly nested and closed again (`<b>`,
 * `<a>`, `<code>`…), or an opening void tag (`<br>`, `<img>`…), which opens
 * nothing. Media before closed tags like these stays put while later blocks
 * stream. Any other raw HTML, a stray or misnested closing tag, or an HTML
 * block other than a lone void tag ends splitting: the rest of the reply
 * renders whole, and the block before it goes in the tail too, so the
 * sanitizer never parses a fragment that starts with raw HTML (it moves a
 * leading `<title>`, `<template>` or `<noscript>` out of the body).
 */
export function streamingSplit(text: string): number {
	const tokens = chatMarked.lexer(text);
	if (Object.keys(tokens.links).length > 0) return 0;
	const open: string[] = [];
	let offset = 0;
	let last = 0;
	let before = 0;
	for (const token of tokens) {
		// The lexer normalises line endings; offsets hold only while each raw is the source.
		if (!text.startsWith(token.raw, offset)) return 0;
		if (token.type !== 'space' && open.length === 0) {
			before = last;
			last = offset;
		}
		if (!followTags(token, open)) return last === offset ? before : last;
		offset += token.raw.length;
	}
	return last;
}

/** Inline elements the HTML parser closes where their closing tag says, holding nothing raw. */
const PLAIN_TAGS = new Set([
	'a', 'abbr', 'b', 'bdi', 'bdo', 'cite', 'code', 'data', 'del', 'dfn', 'em', 'i', 'ins', 'kbd',
	'mark', 'q', 's', 'samp', 'small', 'span', 'strong', 'sub', 'sup', 'time', 'u', 'var'
]);
/** Void elements: an opening tag holds nothing and leaves nothing open. */
const VOID_TAGS = new Set(['br', 'wbr', 'hr', 'img']);
/** A whole tag: optional slash, the exact name, then attributes or the end. */
const TAG_RE = /^<(\/?)([a-z][a-z0-9-]*)(?=[\s/>])[^<>]*>$/i;

/**
 * Walk a token's raw HTML in document order, keeping the stack of plain
 * elements still open. False at the first tag that is not plain, a closing
 * tag that is not the bare `</name>` of the innermost open element, or an HTML block that
 * is more than one opening void tag. A plain walk that stops at the first
 * hit: `marked.walkTokens` collects results by concatenation, which is
 * quadratic on a long list.
 */
function followTags(token: Token, open: string[]): boolean {
	if (token.type === 'html') {
		const tag = TAG_RE.exec(token.raw.trim());
		if (!tag) return false;
		const name = tag[2].toLowerCase();
		if (tag[1]) {
			// Only the bare form: marked keeps a link open past `</a >`, the parser does not.
			if (token.block || open[open.length - 1] !== name || token.raw.trim().toLowerCase() !== `</${name}>`) return false;
			open.pop();
			return true;
		}
		if (VOID_TAGS.has(name)) return true;
		if (token.block || !PLAIN_TAGS.has(name)) return false;
		open.push(name);
		return true;
	}
	const t = token as Token & {
		tokens?: Token[]; items?: Tokens.ListItem[];
		header?: Tokens.TableCell[]; rows?: Tokens.TableCell[][];
	};
	if (t.tokens) for (const child of t.tokens) if (!followTags(child, open)) return false;
	if (t.items) for (const item of t.items) if (!followTags(item, open)) return false;
	if (t.header) for (const cell of t.header) for (const child of cell.tokens) if (!followTags(child, open)) return false;
	if (t.rows) for (const row of t.rows) for (const cell of row) for (const child of cell.tokens) if (!followTags(child, open)) return false;
	return true;
}

/**
 * Parse chat markdown to HTML. The caller still sanitises the result.
 *
 * @param highlight - false for the still-streaming tail (a fence that has not
 *   closed yet must not be tokenised, and re-tokenising per token would be the
 *   single most expensive thing in the stream loop).
 * @param media - render media markdown as players (the chat reply); omitted,
 *   images and links render as plain markdown.
 */
export function renderChatMarkdown(text: string, highlight: boolean, media?: ChatMedia): string {
	_highlightEnabled = highlight;
	_media = media ?? null;
	try {
		return chatMarked.parse(media ? collapseRawBase64(text) : text, { async: false }) as string;
	} finally {
		_highlightEnabled = true;
		_media = null;
	}
}
