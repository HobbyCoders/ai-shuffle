import { Marked, type Token } from 'marked';
import { withAuth } from '$lib/api/runtime';
import { chatFileUrl } from './chat-files';

export interface HtmlPreviewLink {
	href: string;
	path: string;
	filename: string;
}

export const MAX_HTML_PREVIEW_BYTES = 2 * 1024 * 1024;
/** Never add allow-same-origin: generated scripts must have an opaque origin. */
export const HTML_PREVIEW_SANDBOX = 'allow-scripts';

const FILE_ROUTE = '/api/files/by-path';
// Survives transcript re-renders/remounts, scoped to worker URL and session.
const missingPreviews = new Set<string>();

export function retryHtmlPreview(href: string, sessionId?: string | null): void {
	missingPreviews.delete(chatFileUrl(href, sessionId));
}

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
export async function loadHtmlPreview(href: string, signal: AbortSignal, sessionId?: string | null): Promise<string> {
	const link = htmlPreviewLink(href);
	if (!link) throw new Error('Only local HTML file links can be previewed.');
	signal.throwIfAborted();
	const url = chatFileUrl(link.href, sessionId);
	if (missingPreviews.has(url)) throw new Error('Could not load this HTML file (HTTP 404).');
	const response = await fetch(url, withAuth({
		signal,
		redirect: 'error',
		cache: 'no-store'
	}));
	if (!response.ok) {
		if (response.status === 404 && !signal.aborted) {
			if (missingPreviews.size >= 200) missingPreviews.delete(missingPreviews.values().next().value!);
			missingPreviews.add(url);
		}
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

// ── Frame protocol ─────────────────────────────────────────────────────────
// Names follow the MCP Apps conventions where one exists. Everything the frame
// sends is page-controlled: the page's own scripts can post any of these, so
// the parent validates shape, sender and (for links and images) user intent.

export const HTML_PREVIEW_METHODS = {
	/** frame → app: the document's content height in CSS pixels. */
	sizeChanged: 'ui/notifications/size-changed',
	/** frame → app: open an http(s) link in a new browser tab. */
	openLink: 'ui/open-link',
	/** frame → app: show a clicked inline (data:) image in the app's viewer. */
	openImage: 'ai-shuffle/open-image',
	/** frame → app: Escape pressed inside the page and not handled by it. */
	escape: 'ai-shuffle/escape',
	/** frame → app: an inline page's #anchor was clicked; scroll the transcript to it. */
	scrollTo: 'ai-shuffle/scroll-to',
	/** frame → app: the pointer entered or left the page (reveals the controls). */
	hover: 'ai-shuffle/hover',
	/** app → frame: live theme variables and scroll mode. */
	hostContextChanged: 'ui/notifications/host-context-changed'
} as const;

export type HtmlPreviewMessage =
	| { kind: 'size'; height: number; viewport?: number }
	| { kind: 'link'; url: string }
	| { kind: 'image'; src: string; name: string }
	| { kind: 'escape' }
	| { kind: 'hover'; inside: boolean }
	| { kind: 'scroll'; top: number };

const MAX_IMAGE_SOURCE_LENGTH = 12 * 1024 * 1024;

/** Parse a frame message. Anything that is not exactly one known shape is null. */
export function readHtmlPreviewMessage(data: unknown): HtmlPreviewMessage | null {
	if (typeof data !== 'object' || data === null) return null;
	const { jsonrpc, method, params } = data as Record<string, unknown>;
	if (jsonrpc !== '2.0' || typeof method !== 'string') return null;
	const p = (typeof params === 'object' && params !== null ? params : {}) as Record<string, unknown>;
	switch (method) {
		case HTML_PREVIEW_METHODS.sizeChanged: {
			const { height, viewport } = p;
			if (typeof height !== 'number' || !Number.isFinite(height) || height < 0) return null;
			return typeof viewport === 'number' && Number.isFinite(viewport) && viewport >= 0
				? { kind: 'size', height, viewport }
				: { kind: 'size', height };
		}
		case HTML_PREVIEW_METHODS.openLink: {
			if (typeof p.url !== 'string' || p.url.length > 8192) return null;
			const url = safeExternalUrl(p.url);
			return url ? { kind: 'link', url } : null;
		}
		case HTML_PREVIEW_METHODS.openImage: {
			const src = p.src;
			if (typeof src !== 'string' || src.length > MAX_IMAGE_SOURCE_LENGTH) return null;
			if (!/^data:image\/(?:png|jpe?g|gif|webp|avif|svg\+xml)[;,]/i.test(src)) return null;
			const name = typeof p.name === 'string' ? p.name.trim().slice(0, 200) : '';
			return { kind: 'image', src, name };
		}
		case HTML_PREVIEW_METHODS.escape:
			return { kind: 'escape' };
		case HTML_PREVIEW_METHODS.scrollTo:
			return typeof p.top === 'number' && Number.isFinite(p.top) && p.top >= 0 ? { kind: 'scroll', top: p.top } : null;
		case HTML_PREVIEW_METHODS.hover:
			return typeof p.inside === 'boolean' ? { kind: 'hover', inside: p.inside } : null;
		default:
			return null;
	}
}

/** Only absolute http(s) URLs without credentials leave the frame. */
export function safeExternalUrl(raw: string): string | null {
	if (/[\u0000-\u001f\u007f]/.test(raw)) return null;
	try {
		const url = new URL(raw);
		if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
		if (url.username || url.password) return null;
		return url.href;
	} catch {
		return null;
	}
}

/**
 * Accept a window message only from this exact frame. A sandboxed srcdoc
 * document without allow-same-origin always has the opaque origin "null";
 * anything else did not come from the preview's own document.
 */
export function acceptHtmlPreviewMessage(event: MessageEvent, frame: HTMLIFrameElement | null | undefined): HtmlPreviewMessage | null {
	const source = frame?.contentWindow;
	if (!source || event.source !== source || event.origin !== 'null') return null;
	return readHtmlPreviewMessage(event.data);
}

/**
 * Links and images open only while the reader is using this frame: it has
 * focus, and (where the browser exposes it) a click or key just happened.
 * A page can take focus by script, so this stops opens on load or on a timer,
 * not a page that waits for the reader's next click.
 */
export function previewHasUserIntent(frame: HTMLIFrameElement | null | undefined, doc: Document = document, nav: Navigator = navigator): boolean {
	if (!frame || doc.activeElement !== frame) return false;
	const activation = (nav as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
	return activation ? activation.isActive === true : true;
}

// ── Theme ──────────────────────────────────────────────────────────────────

export interface HtmlPreviewTheme {
	appearance: 'light' | 'dark';
	variables: Record<string, string>;
}

const CSS_VARIABLE_NAME = /^--[a-z0-9-]{1,64}$/;

/** Values land inside a <style> element: never let one close the rule or the element. */
export function cleanThemeValue(value: string): string {
	return value.replace(/[;{}<>\\]/g, '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 240);
}

function themeRule(theme: HtmlPreviewTheme): string {
	let css = `:where(:root){color-scheme:${theme.appearance === 'light' ? 'light' : 'dark'};`;
	for (const [name, value] of Object.entries(theme.variables)) {
		if (CSS_VARIABLE_NAME.test(name)) css += `${name}:${cleanThemeValue(value)};`;
	}
	return `${css}}`;
}

// Zero-specificity selectors and placement before the page's own styles mean
// any rule the page writes wins over these defaults. The 14px root size is
// deliberate: it matches the reply text, so an unstyled or rem-based page
// reads as part of the message (rem units come out 12.5% smaller than in a
// browser tab; a page that sets its own root size keeps it).
const BASE_CSS = ':where(html){background:transparent;color:var(--foreground,CanvasText);font-family:var(--font-sans,system-ui,sans-serif);font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;overflow-wrap:break-word}'
	+ ':where(body){margin:0}:where(code,kbd,pre,samp){font-family:var(--font-mono,ui-monospace,monospace)}'
	+ ':where(a){color:var(--primary,LinkText)}:where(img){cursor:zoom-in}:where(a img,button img){cursor:inherit}';
// Inline frames grow to their content, so the page itself never scrolls
// vertically; wide content can still scroll sideways.
const INLINE_CSS = ':where(html){overflow-y:hidden;overflow-x:auto;scrollbar-width:thin}';
// An inline page pinned at a fixed height (it sizes itself to its viewport)
// scrolls inside its frame, with a slim scrollbar.
const PINNED_CSS = ':where(html){scrollbar-width:thin}';

// Runs synchronously in <head> before any page script or body. It reports the
// content height (scrollHeight only when the page overflows, so a frame can
// also shrink), applies live theme and scroll-mode updates sent by the parent
// window only, forwards http(s) link clicks and data: image clicks, blocks
// every other navigation and form submission, and passes Escape and
// pointer enter/leave on (the parent cannot see hover inside the frame).
// An inline page cannot scroll, so its #anchor links ask the parent to
// scroll the transcript to the target instead.
const BOOTSTRAP_SCRIPT = `(function(){
var P=window.parent;var M=${JSON.stringify(HTML_PREVIEW_METHODS)};
var T=document.getElementById('ais-host-theme'),S=document.getElementById('ais-host-mode'),INLINE=${JSON.stringify(INLINE_CSS)},PINNED=${JSON.stringify(PINNED_CSS)};
var framed=P&&P!==window,last=-1,linkId=0;
function post(m){if(framed)P.postMessage(m,'*');}
function apply(p){if(!p||typeof p!=='object')return;var v=p.styles&&p.styles.variables;
if(T&&v&&typeof v==='object'){var c=':where(:root){color-scheme:'+(p.theme==='light'?'light':'dark')+';';for(var k in v){if(/^--[a-z0-9-]{1,64}$/.test(k)&&typeof v[k]==='string')c+=k+':'+v[k].replace(/[;{}<>\\\\]/g,'').slice(0,240)+';';}T.textContent=c+'}';}
if(S&&typeof p.scrollable==='boolean')S.textContent=p.scrollable?PINNED:INLINE;}
window.addEventListener('message',function(e){if(e.source!==P)return;var d=e.data;if(d&&d.jsonrpc==='2.0'&&d.method===M.hostContextChanged)apply(d.params);});
document.addEventListener('click',function(e){
var t=e.target instanceof Element?e.target:null,a=t?t.closest('a, area'):null;if(!a)return;
var h=a.getAttribute('href')||'';if(h.charAt(0)==='#'){
if(!framed||!S||S.textContent!==INLINE||h.length<2)return;var id;try{id=decodeURIComponent(h.slice(1));}catch(x){return;}
var el=document.getElementById(id)||document.getElementsByName(id)[0];if(!el)return;e.preventDefault();
post({jsonrpc:'2.0',method:M.scrollTo,params:{top:Math.max(0,el.getBoundingClientRect().top+window.scrollY)}});return;}
e.preventDefault();if(!e.isTrusted||!h)return;var u;try{u=new URL(h);}catch(x){return;}
if(u.protocol!=='http:'&&u.protocol!=='https:')return;
post({jsonrpc:'2.0',id:'ais-link-'+(++linkId),method:M.openLink,params:{url:u.href}});},true);
document.addEventListener('submit',function(e){e.preventDefault();},true);
document.addEventListener('click',function(e){var t=e.target;
if(!e.isTrusted||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||!t||t.tagName!=='IMG'||t.closest('a[href],button'))return;
var u=t.currentSrc||t.src||'';if(!/^data:image\\//i.test(u))return;
setTimeout(function(){if(!e.defaultPrevented)post({jsonrpc:'2.0',method:M.openImage,params:{src:u,name:t.alt||t.title||''}});});});
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&e.isTrusted&&!e.defaultPrevented)post({jsonrpc:'2.0',method:M.escape});});
var R=document.documentElement;R.addEventListener('mouseenter',function(){post({jsonrpc:'2.0',method:M.hover,params:{inside:true}});});
R.addEventListener('mouseleave',function(){post({jsonrpc:'2.0',method:M.hover,params:{inside:false}});});
if(!framed)return;var ready=false,lastV=0;
function measure(){var r=document.documentElement,v=window.innerHeight;if(!ready||!r||!v)return;var h=Math.ceil((r.scrollHeight>r.clientHeight?r.scrollHeight:r.getBoundingClientRect().height)+Math.max(0,v-r.clientHeight));
if(!h||(h===last&&v===lastV))return;last=h;lastV=v;post({jsonrpc:'2.0',method:M.sizeChanged,params:{height:h,viewport:v}});}
var o=window.ResizeObserver?new ResizeObserver(measure):null;if(o)o.observe(document.documentElement);
document.addEventListener('DOMContentLoaded',function(){ready=true;if(o&&document.body)o.observe(document.body);requestAnimationFrame(measure);});
window.addEventListener('load',measure);window.addEventListener('resize',measure);
})();`;

export interface HtmlPreviewDocumentOptions {
	/** Snapshot of the app theme for the first paint; later changes arrive by message. */
	theme?: HtmlPreviewTheme | null;
	/** Inline frames fit their content and never scroll vertically. */
	display?: 'inline' | 'fullscreen';
}

/**
 * Only assign this to srcdoc on an iframe with HTML_PREVIEW_SANDBOX.
 * Never render it with {@html}, or load the original API URL in an iframe.
 * CSP precedes all source bytes; source-supplied policies cannot loosen it.
 * The host theme, base styles and bootstrap follow the CSP and precede the
 * source, so the page's own CSS and scripts run after (and win over) them.
 * This is standalone rendering, not a browser/network security boundary:
 * browsers without navigate-to support can still self-navigate via scripts
 * or meta refresh. Opaque-origin sandboxing protects the application itself.
 */
export function buildHtmlPreviewDocument(source: string, options: HtmlPreviewDocumentOptions = {}): string {
	const theme = options.theme ? themeRule(options.theme) : '';
	const mode = options.display === 'fullscreen' ? '' : INLINE_CSS;
	return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}"><meta name="referrer" content="no-referrer">`
		+ `<style id="ais-host-theme">${theme}</style><style id="ais-host-base">${BASE_CSS}</style><style id="ais-host-mode">${mode}</style>`
		+ `<script>${BOOTSTRAP_SCRIPT}</script>${source}`;
}

export function hostContextMessage(theme: HtmlPreviewTheme, scrollable?: boolean) {
	return {
		jsonrpc: '2.0',
		method: HTML_PREVIEW_METHODS.hostContextChanged,
		params: {
			theme: theme.appearance,
			styles: { variables: theme.variables },
			...(scrollable === undefined ? {} : { scrollable })
		}
	} as const;
}

// ── Fit to content ─────────────────────────────────────────────────────────

export const HTML_PREVIEW_MIN_HEIGHT = 64;
export const HTML_PREVIEW_MAX_HEIGHT = 12_000;
/** Reserved while the page loads and measures, so the transcript does not jump. */
export const HTML_PREVIEW_INITIAL_HEIGHT = 320;
/** A page whose height follows its frame gets this, and scrolls inside. */
export const HTML_PREVIEW_FALLBACK_HEIGHT = 540;
/** How far a probe moves the frame to test whether the page follows it. */
export const HTML_PREVIEW_PROBE_STEP = 96;
/** How long a probe waits for the page to answer before calling it independent. */
/** Generous: browsers throttle offscreen cross-origin frames, which answer late. */
export const HTML_PREVIEW_PROBE_MS = 600;
const SUSPICION_STREAK = 3;
/** Sub-pixel layout can put innerHeight a pixel or two off the frame height. */
const STALE_TOLERANCE = 2;

export type HtmlPreviewSizerMode = 'measuring' | 'probing' | 'fit' | 'fixed';
export type HtmlPreviewSizerAction = 'none' | 'resize' | 'probe' | 'fixed';

const clampHeight = (height: number) => Math.min(HTML_PREVIEW_MAX_HEIGHT, Math.max(HTML_PREVIEW_MIN_HEIGHT, Math.ceil(height)));

/**
 * Decides the inline frame height from the page's size reports.
 *
 * A page that sizes itself to its viewport (100vh, height:100% on html/body,
 * min-height:100vh plus padding, calc(100vh - 16px)) moves every time its
 * frame moves. The sizer catches that with a probe: it grows the frame by
 * PROBE_STEP and watches whether the reported height moves with it. It probes
 * on the first report when that equals the frame height, and when the last
 * few reports were each either taller than their frame or moved by as much as
 * the frame had just moved. A page that follows the probe is pinned at
 * FALLBACK_HEIGHT and scrolls inside; one that does not (an animation, a
 * chart drawing in) goes on fitting, and needs a longer streak to be probed
 * again.
 */
export class HtmlPreviewSizer {
	height: number;
	mode: HtmlPreviewSizerMode;
	private probeBase = { height: 0, viewport: 0 };
	private previous: { height: number; viewport: number } | null = null;
	private streak = 0;
	private threshold = SUSPICION_STREAK;
	/** Started from a remembered pin: the first mismatch is probed at once. */
	private suspect = false;

	/**
	 * `height` reserves a remembered size and starts fitting from it. A
	 * remembered pin (`fixed`) is never trusted as is: the page may have been
	 * revised, so it reserves the pinned height and probes again.
	 * `reserve` keeps a height while the page is measured from scratch.
	 */
	constructor(initial: { height?: number; fixed?: boolean; reserve?: number } = {}) {
		if (initial.fixed) {
			this.mode = 'measuring';
			this.height = HTML_PREVIEW_FALLBACK_HEIGHT;
			this.suspect = true;
		} else if (initial.height) {
			this.mode = 'fit';
			this.height = clampHeight(initial.height);
		} else {
			this.mode = 'measuring';
			this.height = clampHeight(initial.reserve ?? HTML_PREVIEW_INITIAL_HEIGHT);
		}
	}

	/** True once the frame should be shown (it has a height worth showing). */
	get settled(): boolean { return this.mode === 'fit' || this.mode === 'fixed'; }

	/** Settled and not in the middle of a suspicious run: a size worth remembering. */
	get steady(): boolean { return this.mode === 'fixed' || (this.mode === 'fit' && this.streak === 0); }

	/**
	 * Apply a size report. `viewport` is the frame height the page measured
	 * against (it can lag behind resizes); without it, the current height.
	 */
	report(raw: number, viewport?: number): HtmlPreviewSizerAction {
		// Zero (height or viewport) is a document that has not laid out in its
		// frame yet, not a real measurement.
		if (this.mode === 'fixed' || !Number.isFinite(raw) || raw <= 0 || viewport === 0) return 'none';
		const height = Math.ceil(raw);
		const measured = viewport !== undefined && Number.isFinite(viewport) && viewport > 0 ? Math.round(viewport) : this.height;
		// Measured in a frame size that has since changed: the page measures
		// again once the resize reaches it (the bootstrap reports on resize).
		if (Math.abs(measured - this.height) > STALE_TOLERANCE) return 'none';
		const previous = this.previous;
		this.previous = { height, viewport: measured };
		if (this.mode === 'probing') {
			const moved = measured - this.probeBase.viewport;
			if (moved > 0 && height - this.probeBase.height >= moved * 0.9) return this.pin();
			this.mode = 'fit';
			this.threshold *= 2;
			this.streak = 0;
			return this.fit(height);
		}
		if (this.mode === 'measuring') {
			if (Math.abs(height - measured) <= 1 || (this.suspect && height !== measured)) return this.probe(height, measured);
			this.mode = 'fit';
			return this.fit(height);
		}
		// Two signatures of a page that follows its frame (or of content still
		// growing): it is taller than the frame it was measured in, or it moved
		// by as much as the frame did since the last report (a page sized
		// calc(100vh - N) shrinks in step with every fit). Timing is no guide:
		// an offscreen frame is throttled and answers slowly. The probe tells
		// a follower from content that changes on its own.
		const frameMoved = previous ? measured - previous.viewport : 0;
		const tracks = previous !== null && Math.abs(frameMoved) > 1
			&& Math.abs(height - previous.height - frameMoved) <= STALE_TOLERANCE;
		this.streak = height > measured + 1 || tracks ? this.streak + 1 : 0;
		if (Math.abs(height - this.height) <= 1) return 'none';
		if (this.streak >= this.threshold) return this.probe(height, measured);
		if (height > HTML_PREVIEW_MAX_HEIGHT) return this.pin();
		return this.fit(height);
	}

	/** The probe got no answer: the page's height does not depend on its frame. */
	probeExpired(): HtmlPreviewSizerAction {
		if (this.mode !== 'probing') return 'none';
		this.mode = 'fit';
		this.threshold *= 2;
		this.streak = 0;
		return this.fit(this.probeBase.height);
	}

	/** The page never reported a size (its bootstrap could not run): show it scrollable. */
	giveUp(): HtmlPreviewSizerAction {
		if (this.settled) return 'none';
		if (this.mode === 'probing') return this.probeExpired();
		return this.pin();
	}

	private fit(height: number): HtmlPreviewSizerAction {
		const next = clampHeight(height);
		if (next === this.height) return 'none';
		this.height = next;
		return 'resize';
	}

	private probe(height: number, viewport: number): HtmlPreviewSizerAction {
		this.mode = 'probing';
		this.suspect = false;
		this.probeBase = { height, viewport };
		this.height = clampHeight(Math.max(this.height, height) + HTML_PREVIEW_PROBE_STEP);
		return 'probe';
	}

	private pin(): HtmlPreviewSizerAction {
		this.mode = 'fixed';
		this.height = HTML_PREVIEW_FALLBACK_HEIGHT;
		return 'fixed';
	}
}

// Last known layout per file and viewer, so a remounted message reserves the
// right height at once instead of re-measuring from the loading height.
export interface RememberedPreviewSize {
	height: number;
	fixed: boolean;
	/** Digest of the source this size was measured for. */
	digest: string;
}
const knownSizes = new Map<string, RememberedPreviewSize>();
export function rememberedPreviewSize(key: string) { return knownSizes.get(key); }
export function forgetPreviewSize(key: string) { knownSizes.delete(key); }
/** Cheap content fingerprint (FNV-1a over UTF-16 units plus the length). */
export function previewDigest(source: string): string {
	let hash = 0x811c9dc5;
	for (let i = 0; i < source.length; i++) {
		hash ^= source.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193);
	}
	return `${source.length}:${(hash >>> 0).toString(36)}`;
}
export function rememberPreviewSize(key: string, size: RememberedPreviewSize) {
	knownSizes.delete(key);
	knownSizes.set(key, size);
	if (knownSizes.size > 200) knownSizes.delete(knownSizes.keys().next().value!);
}

// ── Placement in the reply ─────────────────────────────────────────────────

export type HtmlPreviewSegment =
	| { kind: 'html'; key: string; html: string }
	| { kind: 'preview'; key: string; preview: HtmlPreviewLink };

/** The by-path file a rendered anchor points at, after chat file routing. */
function renderedPreviewPath(href: string | null): string | null {
	if (!href) return null;
	try {
		const url = new URL(href, 'https://preview.invalid');
		if (!url.pathname.endsWith(FILE_ROUTE)) return null;
		return url.searchParams.get('path');
	} catch {
		return null;
	}
}

function serializeNode(node: Node): string {
	if (node.nodeType === Node.ELEMENT_NODE) return (node as Element).outerHTML;
	if (node.nodeType === Node.TEXT_NODE) {
		return (node.textContent ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
	}
	return '';
}

/**
 * Split sanitized reply HTML so each preview follows the top-level block
 * (paragraph, list, table, quote) that links to it. Previews whose link is not
 * found in the markup go after the reply. The input is already-sanitized
 * markup from the chat renderer; this only cuts it at element boundaries.
 */
export function placeHtmlPreviews(
	html: string,
	previews: HtmlPreviewLink[],
	sanitize: (html: string) => string
): HtmlPreviewSegment[] {
	if (!previews.length) return [{ kind: 'html', key: 'h0', html }];
	const segments: HtmlPreviewSegment[] = [];
	const remaining = new Map(previews.map((preview) => [preview.path, preview]));
	let chunk = '';
	let index = 0;
	// Splitting parses and re-serializes the markup, which a sanitizer's
	// guarantees do not cover (mutation XSS lives in exactly that round
	// trip). Each piece is sanitized again so what reaches {@html} is always
	// fresh sanitizer output.
	const flush = () => {
		if (chunk) segments.push({ kind: 'html', key: `h${index++}`, html: sanitize(chunk) });
		chunk = '';
	};
	if (typeof document !== 'undefined') {
		const template = document.createElement('template');
		template.innerHTML = html;
		for (const node of Array.from(template.content.childNodes)) {
			chunk += serializeNode(node);
			if (!(node instanceof Element) || !remaining.size) continue;
			const anchors = node.matches('a[href]') ? [node] : Array.from(node.querySelectorAll('a[href]'));
			const found: HtmlPreviewLink[] = [];
			for (const anchor of anchors) {
				const path = renderedPreviewPath(anchor.getAttribute('href'));
				const preview = path === null ? undefined : remaining.get(path);
				if (preview) { found.push(preview); remaining.delete(preview.path); }
			}
			if (!found.length) continue;
			flush();
			for (const preview of found) segments.push({ kind: 'preview', key: `p:${preview.path}`, preview });
		}
	} else {
		chunk = html;
	}
	flush();
	for (const preview of remaining.values()) segments.push({ kind: 'preview', key: `p:${preview.path}`, preview });
	return segments;
}

/** A preview link that is still streaming in. Cheap enough to run on every delta. */
export function streamingPreviewName(content: string): string | null {
	const match = /\]\(\s*<?\/api\/files\/by-path\?path=([^)\s>]*\.html?)>?\s*\)/i.exec(content);
	if (!match) return null;
	try {
		return decodeURIComponent(match[1]).split(/[/\\]/).pop() || null;
	} catch {
		return null;
	}
}
