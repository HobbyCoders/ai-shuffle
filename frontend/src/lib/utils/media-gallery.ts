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

export function galleryMediaForTurn(messages: ChatMessage[]): GalleryMedia[] {
	const found = new Map<string, GalleryMedia>();
	function add(kind: 'image' | 'video', raw: string, label: string) {
		const src = resolveMediaUrl(raw);
		if (!safeMediaSource(src) || found.has(src)) return;
		found.set(src, { kind, src, label: label || `${kind === 'image' ? 'Image' : 'Video'} ${found.size + 1}` });
	}
	for (const message of messages) {
		if (message.streaming) continue;
		if (message.type === 'tool_use' && message.toolScreenshot && message.toolStatus !== 'running') {
			add('image', message.toolScreenshot, `Screenshot from ${message.toolName || 'tool'}`);
		}
		if (message.role === 'assistant' && (message.type === 'text' || !message.type)) {
			// Only rendered embeds: fenced/inline code and ordinary text aren't media.
			marked.walkTokens(marked.lexer(message.content || ''), token => {
				if (token.type !== 'image' && token.type !== 'link') return;
				rewriteMediaMarkdown(token.raw, (kind, src, label) => {
					if (kind !== 'audio') add(kind, src, label);
					return '';
				});
			});
		}
	}
	return found.size > 1 ? [...found.values()] : [];
}

/** Suppress only media represented by the gallery; keep prose, audio and links. */
export function rewriteWithGallery(text: string, sources: ReadonlySet<string>): string {
	return rewriteMediaMarkdown(text, (kind, src, label) =>
		sources.has(resolveMediaUrl(src)) ? '' : mediaHtml(kind, src, label)
	);
}
