import type { ChatMessage } from '$lib/stores/types';
import { marked } from 'marked';
import { mediaHtml, resolveMediaUrl, rewriteMediaMarkdown } from './media';

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

type ProseEmbed = readonly [kind: 'image' | 'video', src: string, label: string];

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
		rewriteMediaMarkdown(token.raw, (kind, src, label) => {
			if (kind !== 'audio') embeds.push([kind, src, label]);
			return '';
		});
	});
	const result = embeds.length ? embeds : NO_EMBEDS;
	if (proseEmbeds.size >= PROSE_EMBED_CACHE_MAX) {
		const oldest = proseEmbeds.keys().next().value;
		if (oldest !== undefined) proseEmbeds.delete(oldest);
	}
	proseEmbeds.set(content, result);
	return result;
}

export function galleryMediaForTurn(messages: ChatMessage[], sessionId?: string | null): GalleryMedia[] {
	const found = new Map<string, GalleryMedia>();
	function add(kind: 'image' | 'video', raw: string, label: string) {
		const src = resolveMediaUrl(raw, sessionId);
		if (!safeMediaSource(src) || found.has(src)) return;
		found.set(src, { kind, src, label: label || `${kind === 'image' ? 'Image' : 'Video'} ${found.size + 1}` });
	}
	for (const message of messages) {
		if (message.streaming) continue;
		if (message.type === 'tool_use' && message.toolScreenshot && message.toolStatus !== 'running') {
			add('image', message.toolScreenshot, `Screenshot from ${message.toolName || 'tool'}`);
		}
		if (message.role === 'assistant' && (message.type === 'text' || !message.type) && message.content) {
			for (const [kind, src, label] of embedsOf(message.content)) add(kind, src, label);
		}
	}
	return found.size > 1 ? [...found.values()] : [];
}

/** Suppress only media represented by the gallery; keep prose, audio and links. */
export function rewriteWithGallery(text: string, sources: ReadonlySet<string>, sessionId?: string | null): string {
	return rewriteMediaMarkdown(text, (kind, src, label) =>
		sources.has(resolveMediaUrl(src, sessionId)) ? '' : mediaHtml(kind, src, label)
	);
}
