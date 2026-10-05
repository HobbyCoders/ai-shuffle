/**
 * Shared media detection + inline-player markup for chat rendering.
 *
 * One source of truth for: which URLs are media, what kind they are, and the
 * HTML block (player + download + lightbox hook) that represents them. Used by
 * AssistantMessage (markdown rewrite) and SubagentMessage (tool-result render).
 *
 * The markup here is rendered via Svelte's {@html} and must survive the
 * sanitizer in sanitize.ts — keep the two in sync (allowed tags/attrs, and the
 * `.media-*` class names). Visibility/hover styling lives in app.css.
 */

import { apiUrl } from '$lib/api/runtime';
import { chatFileUrl } from './chat-files';
import { escapeHtml } from './escape-html';

export type MediaKind = 'image' | 'video' | 'audio';

/**
 * Backend-relative media paths (`/api/generated-*`, `/api/files/by-path`) are
 * same-origin on the web UI but must point at the worker when the page is the
 * phone UI in the apps (older Android apps load it from their own origin).
 * Everything else (data:, blob:, absolute) is untouched.
 */
export function resolveMediaUrl(url: string, sessionId?: string | null): string {
	const routed = chatFileUrl(url, sessionId);
	return routed.startsWith('/api/') ? apiUrl(routed) : routed;
}

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif']);
const VIDEO_EXTS = new Set(['mp4', 'webm', 'mov', 'm4v']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'aac']);

/**
 * The file a media URL targets: the `?path=` of a by-path serve route, else the
 * URL path. Decoded the way the server decodes it (`+` is a space in a query),
 * so it reads the same before and after `chatFileUrl` re-encodes the URL for
 * session-owner routing.
 */
function mediaTarget(url: string): { path: string; fromQuery: boolean } {
	const bare = url.split(/[?#]/)[0];
	if (url.startsWith('data:')) return { path: bare, fromQuery: false };
	const query = url.match(/\?([^#]*)/);
	const path = query ? new URLSearchParams(query[1]).get('path') : null;
	if (path) return { path, fromQuery: true };
	try {
		return { path: decodeURIComponent(bare), fromQuery: false };
	} catch {
		return { path: bare, fromQuery: false };
	}
}

/** Extract a lowercase file extension from a URL, handling `?path=` serve routes. */
function extOf(url: string): string {
	const candidate = mediaTarget(url).path;
	const dot = candidate.lastIndexOf('.');
	return dot >= 0 ? candidate.slice(dot + 1).toLowerCase() : '';
}

/**
 * Classify a URL into a media kind, or null if it is not embeddable media.
 * Recognizes data: URIs, the /api/generated-* serve routes, and bare file
 * extensions (covers /api/files/by-path and external/direct URLs).
 */
export function classifyMediaUrl(url: string): MediaKind | null {
	if (!url) return null;
	if (url.startsWith('data:image/')) return 'image';
	if (url.startsWith('data:video/')) return 'video';
	if (url.startsWith('data:audio/')) return 'audio';
	if (url.includes('/api/generated-images/')) return 'image';
	if (url.includes('/api/generated-videos/')) return 'video';
	if (url.includes('/api/generated-audio/')) return 'audio';
	const ext = extOf(url);
	if (IMAGE_EXTS.has(ext)) return 'image';
	if (VIDEO_EXTS.has(ext)) return 'video';
	if (AUDIO_EXTS.has(ext)) return 'audio';
	return null;
}

/**
 * The file name a media URL points at, or null when it names none:
 * `/api/.../by-path?path=.../name.ext` or `/api/.../name.ext`.
 */
export function filenameFromMediaUrl(url: string): string | null {
	if (url.startsWith('data:')) return null;
	const { path, fromQuery } = mediaTarget(url);
	const name = path.split(fromQuery ? /[/\\]/ : '/').pop();
	return name && (fromQuery || name.includes('.')) ? name : null;
}

/** Derive a download filename from a URL when none is supplied. */
function filenameFor(url: string, kind: MediaKind): string {
	const fallback = kind === 'image' ? 'png' : kind === 'video' ? 'mp4' : 'mp3';
	return filenameFromMediaUrl(url) ?? `generated-${kind}.${extOf(url) || fallback}`;
}

const DOWNLOAD_ICON =
	'<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>';

const EXPAND_ICON =
	'<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" /></svg>';

/** The lightbox button's classes; the sanitizer keeps only buttons built from them. */
export const MEDIA_EXPAND_BUTTON_CLASS =
	'media-expand-btn p-1.5 rounded-sm bg-black/60 text-white hover:bg-black/80 transition-colors';

function overlayButtons(url: string, filename: string): string {
	const u = escapeHtml(url);
	const f = escapeHtml(filename);
	return `<div class="media-overlay-buttons absolute top-2 right-2 flex gap-1 transition-opacity">
			<button type="button" class="${MEDIA_EXPAND_BUTTON_CLASS}" title="Expand" aria-label="Expand">${EXPAND_ICON}</button>
			<a href="${u}" download="${f}" class="media-download-btn p-1.5 rounded-sm bg-black/60 text-white hover:bg-black/80 transition-colors no-underline" title="Download" aria-label="Download">${DOWNLOAD_ICON}</a>
		</div>`;
}

/**
 * Build the inline player HTML for a single media item. `label` is used as alt
 * text / aria label; `filename` (optional) names the download.
 */
export function mediaHtml(kind: MediaKind, rawUrl: string, label = '', filename?: string): string {
	const url = resolveMediaUrl(rawUrl);
	const u = escapeHtml(url);
	const alt = escapeHtml(label || `Generated ${kind}`);
	const name = filename || filenameFor(url, kind);

	if (kind === 'image') {
		return `<div class="generated-media-container relative my-4 inline-block">
			<img src="${u}" alt="${alt}" class="media-zoomable max-w-full max-h-[500px] aspect-[auto_3/2] rounded-lg shadow-lg border border-border cursor-zoom-in" />
			${overlayButtons(url, name)}
		</div>`;
	}

	if (kind === 'video') {
		return `<div class="generated-media-container relative my-4 inline-block">
			<video src="${u}" controls playsinline preload="metadata" class="max-w-full max-h-[500px] aspect-[auto_3/2] rounded-lg shadow-lg border border-border">
				Your browser does not support the video tag.
			</video>
			${overlayButtons(url, name)}
		</div>`;
	}

	// audio — no lightbox; player + a download button beside it
	const f = escapeHtml(name);
	return `<div class="generated-media-container generated-audio my-4 flex items-center gap-2 max-w-md rounded-lg border border-border bg-muted/30 px-3 py-2">
		<audio src="${u}" controls preload="metadata" class="flex-1 min-w-0" title="${alt}"></audio>
		<a href="${u}" download="${f}" class="media-download-btn shrink-0 p-1.5 rounded-sm text-muted-foreground hover:text-foreground transition-colors no-underline" title="Download audio" aria-label="Download audio">${DOWNLOAD_ICON}</a>
	</div>`;
}

export interface ResultMedia {
	kind: MediaKind;
	src: string;
	filename?: string;
}

interface ToolResultShape {
	success?: boolean;
	video_url?: string;
	audio_url?: string;
	image_url?: string;
	image_base64?: string;
	audio_base64?: string;
	mime_type?: string;
	filename?: string;
}

/**
 * Extract embeddable media from an AI-tool JSON result string, or null.
 * Prefers explicit *_url fields, falling back to inline base64.
 */
export function extractToolResultMedia(content: string): ResultMedia | null {
	let data: ToolResultShape;
	try {
		data = JSON.parse(content) as ToolResultShape;
	} catch {
		return null;
	}
	if (!data || !data.success) return null;
	if (data.video_url) return { kind: 'video', src: data.video_url, filename: data.filename };
	if (data.audio_url) return { kind: 'audio', src: data.audio_url, filename: data.filename };
	if (data.image_url) return { kind: 'image', src: data.image_url, filename: data.filename };
	if (data.image_base64) {
		return { kind: 'image', src: `data:${data.mime_type || 'image/png'};base64,${data.image_base64}`, filename: data.filename };
	}
	if (data.audio_base64) {
		return { kind: 'audio', src: `data:${data.mime_type || 'audio/mpeg'};base64,${data.audio_base64}`, filename: data.filename };
	}
	return null;
}

// Markdown media patterns. Convention: image syntax ![alt](url) → image;
// link syntax [text](url) → video/audio/other embeddable media.
const IMG_MD = /!\[([^\]]*)\]\(([^)\s]+)\)/g;
const LINK_MD = /(^|[^!])\[([^\]]*)\]\(([^)\s]+)\)/g;
// A single complete media-markdown token (image or link syntax). Used to keep
// the typewriter from revealing a half-typed URL mid-stream.
const MEDIA_TOKEN = /!?\[[^\]]*\]\([^)\s]+\)/g;
// A raw base64 image blob accidentally dumped into prose — collapse it instead
// of rendering ~100k chars of literal text.
const RAW_BASE64 = /"image_base64"\s*:\s*"[A-Za-z0-9+/=]{100,}"/g;
const PLACEHOLDER = '<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">';

/**
 * Rewrite media markdown into inline-player HTML blocks. Used for BOTH the
 * streaming and final render, so a generated image/video/audio appears the
 * instant its complete markdown is in the buffer — no "loading" placeholder.
 * Non-media markdown is left untouched for the markdown parser.
 *
 * Flicker note: the markup is stable, but Svelte's {@html} recreates DOM
 * whenever its string changes. AssistantMessage keeps settled media out of the
 * re-rendered tail (see its prefix/tail split) so a rendered <img> is never
 * torn down and refetched as later text streams in.
 */
export function rewriteMediaMarkdown(
	text: string,
	renderMedia: (kind: MediaKind, url: string, label: string) => string = mediaHtml
): string {
	let out = text.replace(IMG_MD, (match, alt: string, url: string) => {
		const kind = classifyMediaUrl(url);
		return kind ? renderMedia(kind, url, alt) : match;
	});
	out = out.replace(LINK_MD, (match, lead: string, label: string, url: string) => {
		const kind = classifyMediaUrl(url);
		// Only embed video/audio for link syntax; leave image links as links.
		if (kind === 'video' || kind === 'audio') return lead + renderMedia(kind, url, label);
		return match;
	});
	// Never dump a raw base64 image blob as literal prose.
	out = out.replace(RAW_BASE64, `${PLACEHOLDER}Image data omitted</div>`);
	return out;
}

/**
 * Clamp a typewriter reveal length so it never stops inside a complete media
 * markdown token: if `cut` lands mid-token, pull back to the token's start so
 * the media pops in whole (no half-typed URL) once the cursor passes its end.
 * Only complete, embeddable media tokens are clamped — plain links type out
 * normally.
 */
export function clampRevealToMediaBoundary(text: string, cut: number): number {
	MEDIA_TOKEN.lastIndex = 0;
	let m: RegExpExecArray | null;
	while ((m = MEDIA_TOKEN.exec(text)) !== null) {
		const start = m.index;
		if (start >= cut) break; // token starts at/after the cut — not revealed yet
		const end = start + m[0].length;
		if (cut < end) {
			// Cut straddles this token. Clamp only if it is embeddable media.
			const urlMatch = m[0].match(/\(([^)\s]+)\)/);
			if (urlMatch && classifyMediaUrl(urlMatch[1])) return start;
			break;
		}
	}
	return cut;
}
