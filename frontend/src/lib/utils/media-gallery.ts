import type { ChatMessage } from '$lib/stores/types';
import { marked } from 'marked';
import {
	mediaHtml,
	resolveMediaUrl,
	rewriteMediaMarkdown,
	toolResultMedia,
	type MediaKind,
	type ToolResultMediaItem
} from './media';
import { stripUntrustedFence } from './untrusted-fence';

export interface GalleryMedia {
	kind: 'image' | 'video';
	src: string;
	label: string;
}

// Svelte bindings don't pass through the markdown sanitizer. Reject active URLs
// before they reach either the gallery or its download link.
export function safeMediaSource(src: string): boolean {
	return /^(?:https?:\/\/|blob:|data:(?:image|video)\/|\/(?!\/))/i.test(src);
}

/**
 * `kind` is null for a markdown image whose URL names no media kind (an
 * artifact URL, `/api/v1/artifacts/<hash>`): the markdown renderer still draws
 * it as a picture, so a tool row must count it as shown, but it only joins the
 * gallery through the tool row that made it.
 */
type ProseEmbed = readonly [kind: MediaKind | null, src: string, label: string];

/**
 * Media embeds of one finished prose row, before URL resolution. Lexing is the
 * expensive part and depends only on the text, so it runs once per content
 * string: a transcript rebuild re-reads every settled answer, and re-lexing all
 * of history there cost a long session ~1,000 lexer calls per update (#1282).
 * Keyed by the content string itself; the bound keeps a long session's history
 * resident while dropping the oldest entries first.
 */
const proseEmbeds = new Map<string, readonly ProseEmbed[]>();
const PROSE_EMBED_CACHE_MAX = 4000;
const NO_EMBEDS: readonly ProseEmbed[] = [];

function embedsOf(content: string): readonly ProseEmbed[] {
	const cached = proseEmbeds.get(content);
	if (cached) return cached;
	const embeds: ProseEmbed[] = [];
	// Only rendered embeds: fenced/inline code and ordinary text aren't media.
	marked.walkTokens(marked.lexer(content), token => {
		if (token.type !== 'image' && token.type !== 'link') return;
		// Audio is kept here so a tool row can tell its clip is already in the
		// reply; the gallery itself takes only images and video.
		const before = embeds.length;
		rewriteMediaMarkdown(token.raw, (kind, src, label) => {
			embeds.push([kind, src, label]);
			return '';
		});
		if (token.type === 'image' && embeds.length === before && token.href) {
			embeds.push([null, token.href, token.text ?? '']);
		}
	});
	return remember(proseEmbeds, content, embeds.length ? embeds : NO_EMBEDS);
}

function remember<T>(cache: Map<string, T>, key: string, value: T): T {
	if (cache.size >= PROSE_EMBED_CACHE_MAX) {
		const oldest = cache.keys().next().value;
		if (oldest !== undefined) cache.delete(oldest);
	}
	cache.set(key, value);
	return value;
}

const isProse = (message: ChatMessage) =>
	message.role === 'assistant' && (message.type === 'text' || !message.type) && !!message.content;

/**
 * Resolved sources of every media embed in finished assistant prose: what the
 * reply already shows, so a tool row does not show the same picture again.
 */
export function proseMediaSources(messages: readonly ChatMessage[], sessionId?: string | null): Set<string> {
	const sources = new Set<string>();
	for (const message of messages) {
		if (message.streaming || !isProse(message)) continue;
		for (const [, src] of embedsOf(message.content)) sources.add(resolveMediaUrl(src, sessionId));
	}
	return sources;
}

// Parsed per result string, like prose embeds: a transcript rebuild re-reads
// every settled tool row, and most results are not generations at all.
// `toolResultMedia` reads only a JSON object with `success: true` and one of
// these keys, so a result naming neither skips the parse AND the cache: plain
// shell output and file reads must not evict the entries that matter (#792).
const MEDIA_RESULT_KEY = /"(?:(?:video|audio|image|file|model|output)_url|(?:image|audio)_base64|images|outputs)"/;
const toolMediaCache = new Map<string, readonly ToolResultMediaItem[]>();
const NO_MEDIA: readonly ToolResultMediaItem[] = [];

/**
 * What a finished tool call generated (#784): media and files named by a
 * COMPLETED result, unresolved. A running or streaming call, an error, and a
 * refused, skipped, needs-confirmation or choose outcome have none, so they
 * stay text-only rows exactly as before. Same reading as ToolUseMessage.
 */
export function completedToolMedia(message: ChatMessage): readonly ToolResultMediaItem[] {
	if (message.type !== 'tool_use' || message.streaming || !message.toolResult) return NO_MEDIA;
	// Persisted history has used both spellings of a finished call.
	const status = message.toolStatus as string | undefined;
	if (status !== 'complete' && status !== 'completed') return NO_MEDIA;
	const result = message.toolResult;
	if (!result.includes('"success"') || !MEDIA_RESULT_KEY.test(result)) return NO_MEDIA;
	const cached = toolMediaCache.get(result);
	if (cached) return cached;
	const items = toolResultMedia(stripUntrustedFence(result));
	return remember(toolMediaCache, result, items.length ? items : NO_MEDIA);
}

export function galleryMediaForTurn(messages: ChatMessage[], sessionId?: string | null): GalleryMedia[] {
	const found = new Map<string, GalleryMedia>();
	function add(kind: MediaKind | 'file' | null, raw: string, label: string) {
		if (kind !== 'image' && kind !== 'video') return;
		const src = resolveMediaUrl(raw, sessionId);
		if (!safeMediaSource(src) || found.has(src)) return;
		found.set(src, { kind, src, label: label || `${kind === 'image' ? 'Image' : 'Video'} ${found.size + 1}` });
	}
	for (const message of messages) {
		if (message.streaming) continue;
		if (message.type === 'tool_use' && message.toolScreenshot && message.toolStatus !== 'running') {
			add('image', message.toolScreenshot, `Screenshot from ${message.toolName || 'tool'}`);
		}
		// Generated media joins the gallery exactly as a screenshot does (#784).
		for (const item of completedToolMedia(message)) {
			add(item.kind, item.src, item.filename ?? `Generated ${item.kind}`);
		}
		if (isProse(message)) {
			for (const [kind, src, label] of embedsOf(message.content)) add(kind, src, label);
		}
	}
	return found.size > 1 ? [...found.values()] : [];
}

// A markdown image the media rewrite left alone (its URL names no kind), in
// every destination form the lexer accepts: bare or `<angle-bracketed>`, with
// an optional "title", 'title' or (title) (#792).
const PLAIN_IMAGE_MD = /!\[[^\]]*\]\(\s*(?:<([^>\n]*)>|([^)\s]+))(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*\)/g;
// Fenced blocks and inline code spans: their text is code, never an embed.
const CODE_SPAN = /(^|\n)( {0,3})(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\n {0,3}\3[`~]*[ \t]*(?=\n|$)|$)|`+[^`\n]*?`+/g;

/** Suppress only media represented by the gallery; keep prose, audio and links. */
export function rewriteWithGallery(text: string, sources: ReadonlySet<string>, sessionId?: string | null): string {
	const shown = (src: string) => sources.has(resolveMediaUrl(src, sessionId));
	const prose = (part: string) =>
		rewriteMediaMarkdown(part, (kind, src, label) => (shown(src) ? '' : mediaHtml(kind, src, label)))
			// A generated picture the reply embeds by its artifact URL is the
			// same picture the gallery shows from the tool row (#784).
			.replace(PLAIN_IMAGE_MD, (match, bracketed: string | undefined, bare: string | undefined) =>
				shown(bracketed ?? bare ?? '') ? '' : match
			);
	// Code renders exactly as it does without a gallery: nothing is dropped
	// from it, since the lexer never counted it as an embed.
	let out = '';
	let at = 0;
	for (const code of text.matchAll(CODE_SPAN)) {
		out += prose(text.slice(at, code.index)) + rewriteMediaMarkdown(code[0]);
		at = code.index + code[0].length;
	}
	return out + prose(text.slice(at));
}
