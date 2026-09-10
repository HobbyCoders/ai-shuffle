import { Marked, type Token } from 'marked';
import { apiUrl, withAuth } from '$lib/api/runtime';

export interface HtmlPreviewLink {
	href: string;
	path: string;
	filename: string;
}

export const MAX_HTML_PREVIEW_BYTES = 2 * 1024 * 1024;
/** Never add allow-same-origin: generated scripts must have an opaque origin. */
export const HTML_PREVIEW_SANDBOX = 'allow-scripts';

const FILE_ROUTE = '/api/files/by-path';
const markdown = new Marked();

function htmlPreviewLink(href: string): HtmlPreviewLink | null {
	// Check the literal route before URL parsing, which otherwise normalizes
	// backslashes, dot segments and absolute URLs into apparently local paths.
	if (!href.startsWith(`${FILE_ROUTE}?`) || /[\u0000-\u001f\u007f]/.test(href)) return null;
	try {
		const url = new URL(href, 'https://preview.invalid');
		if (url.pathname !== FILE_ROUTE || url.searchParams.getAll('path').length !== 1) return null;
		if ([...url.searchParams.keys()].some((key) => key !== 'path')) return null;
		const path = url.searchParams.get('path')!;
		if (!path || /[\u0000-\u001f\u007f]/.test(path) || !/\.html?$/i.test(path)) return null;
		// Reject malformed percent escapes rather than silently replacing them.
		decodeURIComponent(url.search.slice(1));
		return {
			href: `${FILE_ROUTE}?path=${encodeURIComponent(path)}`,
			path,
			filename: path.split(/[/\\]/).pop()!
		};
	} catch {
		return null;
	}
}

/** Extract links without rewriting markdown or treating code/images as links. */
export function extractHtmlPreviews(content: string, settled = true): HtmlPreviewLink[] {
	if (!settled || !content) return [];
	const previews = new Map<string, HtmlPreviewLink>();
	const imageChildren = new Set<Token>();
	markdown.walkTokens(markdown.lexer(content), (token) => {
		// Marked also tokenizes image alt text, but its nested links are not anchors.
		if (token.type === 'image') {
			markdown.walkTokens(token.tokens ?? [], (child) => { imageChildren.add(child); });
		}
		if (token.type !== 'link' || imageChildren.has(token)) return;
		const preview = htmlPreviewLink(token.href);
		if (preview && !previews.has(preview.path)) previews.set(preview.path, preview);
	});
	return [...previews.values()];
}

/** Fetch through the configured worker/auth policy, never directly into an iframe. */
export async function loadHtmlPreview(href: string, signal: AbortSignal): Promise<string> {
	const link = htmlPreviewLink(href);
	if (!link) throw new Error('Only local HTML file links can be previewed.');
	signal.throwIfAborted();
	const response = await fetch(apiUrl(link.href), withAuth({
		signal,
		redirect: 'error',
		cache: 'no-store'
	}));
	if (!response.ok) {
		await response.body?.cancel();
		throw new Error(`Could not load this HTML file (HTTP ${response.status}).`);
	}
	const type = response.headers.get('content-type')?.split(';')[0].trim().toLowerCase();
	if (type && !['text/html', 'application/xhtml+xml', 'text/plain', 'application/octet-stream'].includes(type)) {
		await response.body?.cancel();
		throw new Error('The file response is not HTML.');
	}
	const tooLarge = () => new Error('This file is too large to preview. The limit is 2 MiB.');
	if (Number(response.headers.get('content-length')) > MAX_HTML_PREVIEW_BYTES) {
		await response.body?.cancel();
		throw tooLarge();
	}
	// Do not fall back to response.text(): it would buffer an unbounded body.
	if (!response.body) throw new Error('The HTML file response is empty.');
	const reader = response.body.getReader();
	const cancel = () => { void reader.cancel().catch(() => {}); };
	signal.addEventListener('abort', cancel, { once: true });
	const decoder = new TextDecoder();
	let bytes = 0;
	let source = '';
	try {
		signal.throwIfAborted();
		while (true) {
			const { done, value } = await reader.read();
			signal.throwIfAborted();
			if (done) break;
			bytes += value.byteLength;
			if (bytes > MAX_HTML_PREVIEW_BYTES) throw tooLarge();
			source += decoder.decode(value, { stream: true });
		}
		source += decoder.decode();
		if (!source.trim()) throw new Error('The HTML file is empty.');
		return source;
	} finally {
		signal.removeEventListener('abort', cancel);
		try { await reader.cancel(); } catch { /* Preserve the load/abort error. */ }
		reader.releaseLock();
	}
}

const PREVIEW_CSP = [
	"default-src 'none'",
	"script-src 'unsafe-inline'",
	"style-src 'unsafe-inline'",
	'img-src data: blob:',
	"connect-src 'none'",
	"font-src 'none'",
	"media-src 'none'",
	"object-src 'none'",
	"frame-src 'none'",
	"child-src 'none'",
	"worker-src 'none'",
	"manifest-src 'none'",
	"form-action 'none'",
	"base-uri 'none'",
	// Not implemented by every browser. The sandbox also forbids top-level
	// navigation, popups and forms, but cannot forbid its own self-navigation.
	"navigate-to 'none'"
].join('; ');

/**
 * Only assign this to srcdoc on an iframe with HTML_PREVIEW_SANDBOX.
 * Never render it with {@html}, or load the original API URL in an iframe.
 * CSP precedes all source bytes; source-supplied policies cannot loosen it.
 * This is standalone rendering, not a browser/network security boundary:
 * browsers without navigate-to support can still self-navigate via scripts
 * or meta refresh. Opaque-origin sandboxing protects the application itself.
 */
export function buildHtmlPreviewDocument(source: string): string {
	return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}"><meta name="referrer" content="no-referrer"><script>
// Block ordinary outgoing links and form submissions before generated handlers.
// These are UX guards, not a substitute for the sandbox and CSP.
document.addEventListener('click', function (event) {
  const link = event.target instanceof Element ? event.target.closest('a, area') : null;
  if (link && !(link.getAttribute('href') || '').startsWith('#')) {
    event.preventDefault();
  }
}, true);
document.addEventListener('submit', function (event) { event.preventDefault(); }, true);
</script>${source}`;
}
