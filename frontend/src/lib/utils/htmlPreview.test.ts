import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	acceptHtmlPreviewMessage,
	buildHtmlPreviewDocument,
	extractHtmlPreviews,
	hostContextMessage,
	HTML_PREVIEW_FALLBACK_HEIGHT,
	HTML_PREVIEW_INITIAL_HEIGHT,
	HTML_PREVIEW_MAX_HEIGHT,
	HTML_PREVIEW_METHODS,
	HTML_PREVIEW_MIN_HEIGHT,
	HTML_PREVIEW_PROBE_STEP,
	HTML_PREVIEW_SANDBOX,
	HTML_PREVIEW_TALL_HEIGHT,
	HtmlPreviewSizer,
	loadHtmlPreview,
	placeHtmlPreviews,
	previewHasUserIntent,
	readHtmlPreviewMessage,
	retryHtmlPreview,
	safeExternalUrl,
	streamingPreviewName,
	MAX_HTML_PREVIEW_BYTES
} from './htmlPreview';

const href = '/api/files/by-path?path=%2Fwork%2Fdemo.html';
const link = `[Open demo](${href})`;
const fetchMock = vi.fn();
const theme = { appearance: 'dark' as const, variables: { '--background': 'oklch(0.1 0 0)', '--foreground': 'white' } };

afterEach(() => {
	retryHtmlPreview(href);
	vi.unstubAllGlobals();
	vi.clearAllMocks();
	delete window.__SHUFFLE_MOBILE__;
});

describe('HTML preview markdown links', () => {
	it('recognizes HTML/HTM files, nested labels, references, tables and lists', () => {
		const source = `- [**Demo**](${href})

[Other][other]

[other]: /api/files/by-path?path=C%3A%5Cwork%5Cother.HTM "Page"

| Site |
| --- |
| [Table](/api/files/by-path?path=/work/table.html) |`;
		expect(extractHtmlPreviews(source)).toEqual([
			{ href, path: '/work/demo.html', filename: 'demo.html' },
			{ href: '/api/files/by-path?path=C%3A%5Cwork%5Cother.HTM', path: 'C:\\work\\other.HTM', filename: 'other.HTM' },
			{ href: '/api/files/by-path?path=%2Fwork%2Ftable.html', path: '/work/table.html', filename: 'table.html' }
		]);
	});

	it('deduplicates decoded paths, removes fragments and preserves source order', () => {
		expect(extractHtmlPreviews(`${link} [Again](/api/files/by-path?path=/work/demo.html#demo) ${link}`)).toHaveLength(1);
	});

	it('decodes spaces, parentheses and Unicode in filenames', () => {
		const path = '/work/mock up (新).html';
		expect(extractHtmlPreviews(`[mock](/api/files/by-path?path=${encodeURIComponent(path)})`)[0].filename).toBe('mock up (新).html');
	});

	it('accepts literal Windows paths and spaces inside angle-bracket destinations', () => {
		const path = String.raw`C:\work\my demo.html`;
		expect(extractHtmlPreviews(`[mock](</api/files/by-path?path=${path}>)`)[0]).toEqual({
			href: `/api/files/by-path?path=${encodeURIComponent(path)}`, path, filename: 'my demo.html'
		});
	});

	it('ignores fenced/indented/inline code, images, raw HTML and unused references', () => {
		const source = '```md\n' + link + '\n```\n\n    ' + link + '\n\n`' + link + '`\n\n!' + link +
			`\n\n<a href="${href}">Raw HTML</a>\n\n[unused]: ${href}`;
		expect(extractHtmlPreviews(source)).toEqual([]);
	});

	it('ignores links nested inside image alt text', () => {
		expect(extractHtmlPreviews(`![${link}](image.png)`)).toEqual([]);
	});

	it('does not extract partial or unsettled streaming links', () => {
		expect(extractHtmlPreviews(link, false)).toEqual([]);
		expect(extractHtmlPreviews(`[Open](${href}`)).toEqual([]);
	});

	it.each([
		'https://example.com/api/files/by-path?path=/work/demo.html',
		'//example.com/api/files/by-path?path=/work/demo.html',
		'javascript:alert(1)',
		'/api/files/../files/by-path?path=/work/demo.html',
		'/api/files/by-path/extra?path=/work/demo.html',
		'/api/files/by-path?path=/work/demo.pdf',
		'/api/files/by-path?path=/work/demo.html.txt',
		'/api/files/by-path?path=/work/demo.html&path=/work/other.html',
		'/api/files/by-path?path=/work/demo.html&redirect=https://example.com',
		'/api/files/by-path?path=/work/%00demo.html',
		'/api/files/by-path?path=/work/%ZZdemo.html',
		'/api/files/by-path?path=/work/demo.html%0A',
		'/api/generated-images/by-path?path=/work/demo.html',
		'/work/demo.html'
	])('rejects non-local/non-HTML/ambiguous destination %s', (url) => {
		expect(extractHtmlPreviews(`[File](<${url}>)`)).toEqual([]);
	});
});

describe('HTML preview source loading', () => {
	function respond(response: Response) {
		vi.stubGlobal('fetch', fetchMock);
		fetchMock.mockResolvedValueOnce(response);
	}

	it('uses authenticated API fetching with cancellation, no cache and no redirects', async () => {
		const controller = new AbortController();
		respond(new Response('<h1>Demo</h1>', { headers: { 'content-type': 'text/html; charset=utf-8' } }));
		await expect(loadHtmlPreview(href, controller.signal)).resolves.toBe('<h1>Demo</h1>');
		expect(fetchMock).toHaveBeenCalledWith(href, {
			signal: controller.signal, redirect: 'error', cache: 'no-store', credentials: 'include'
		});
	});

	it('routes preview source to its owning session', async () => {
		respond(new Response('<h1>Remote</h1>'));
		await loadHtmlPreview(href, new AbortController().signal, 'remote-chat');
		expect(fetchMock.mock.calls[0][0]).toContain('session_id=remote-chat');
	});

	it('uses the configured worker and bearer auth in the bundled runtime', async () => {
		window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://worker.example', token: 'test-preview-token' };
		respond(new Response('demo'));
		await loadHtmlPreview(href, new AbortController().signal);
		expect(fetchMock).toHaveBeenCalledWith(`https://worker.example${href}`, expect.objectContaining({
			credentials: 'omit', headers: { Authorization: 'Bearer test-preview-token' }
		}));
	});

	it('revalidates destinations before fetching', async () => {
		vi.stubGlobal('fetch', fetchMock);
		await expect(loadHtmlPreview('https://example.com/demo.html', new AbortController().signal)).rejects.toThrow('Only local');
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it.each([401, 403, 404, 500])('reports HTTP %s without showing server response HTML', async (status) => {
		respond(new Response('<script>serverError()</script>', { status }));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow(`HTTP ${status}`);
	});

	it('does not retry a missing embed on remount, but allows explicit refresh', async () => {
		const missing = '/api/files/by-path?path=%2Fwork%2Fmissing.html';
		respond(new Response('missing', { status: 404 }));
		await expect(loadHtmlPreview(missing, new AbortController().signal, 'chat')).rejects.toThrow('HTTP 404');
		await expect(loadHtmlPreview(missing, new AbortController().signal, 'chat')).rejects.toThrow('HTTP 404');
		expect(fetchMock).toHaveBeenCalledTimes(1);
		respond(new Response('new owner file'));
		await expect(loadHtmlPreview(missing, new AbortController().signal, 'other-chat')).resolves.toBe('new owner file');
		retryHtmlPreview(missing, 'chat');
		respond(new Response('restored'));
		await expect(loadHtmlPreview(missing, new AbortController().signal, 'chat')).resolves.toBe('restored');
		expect(fetchMock).toHaveBeenCalledTimes(3);
	});

	it('rejects a non-HTML response type', async () => {
		respond(new Response('{}', { headers: { 'content-type': 'application/json' } }));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('not HTML');
	});

	it('rejects a declared oversized file without reading its body', async () => {
		const cancel = vi.fn();
		const body = new ReadableStream({ cancel });
		respond(new Response(body, { headers: { 'content-length': String(MAX_HTML_PREVIEW_BYTES + 1) } }));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('2 MiB');
		expect(cancel).toHaveBeenCalled();
	});

	it('enforces the streamed byte limit even when content-length lies', async () => {
		const cancel = vi.fn();
		const body = new ReadableStream({
			start(controller) { controller.enqueue(new Uint8Array(MAX_HTML_PREVIEW_BYTES + 1)); }, cancel
		});
		respond(new Response(body, { headers: { 'content-length': '1' } }));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('2 MiB');
		expect(cancel).toHaveBeenCalled();
	});

	it('accepts exactly the byte limit and decodes UTF-8 across chunks', async () => {
		const bytes = new TextEncoder().encode('新' + 'a'.repeat(MAX_HTML_PREVIEW_BYTES - 3));
		const body = new ReadableStream({ start(controller) {
			controller.enqueue(bytes.slice(0, 1));
			controller.enqueue(bytes.slice(1));
			controller.close();
		} });
		respond(new Response(body));
		await expect(loadHtmlPreview(href, new AbortController().signal)).resolves.toBe('新' + 'a'.repeat(MAX_HTML_PREVIEW_BYTES - 3));
	});

	it('does not fetch when already aborted', async () => {
		vi.stubGlobal('fetch', fetchMock);
		const controller = new AbortController();
		controller.abort();
		await expect(loadHtmlPreview(href, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('cancels a pending body read on abort', async () => {
		const cancel = vi.fn();
		const controller = new AbortController();
		let started!: () => void;
		const reading = new Promise<void>((resolve) => { started = resolve; });
		const body = new ReadableStream({ pull() { started(); }, cancel }, { highWaterMark: 0 });
		respond(new Response(body));
		const loading = loadHtmlPreview(href, controller.signal);
		await reading;
		controller.abort();
		await expect(loading).rejects.toMatchObject({ name: 'AbortError' });
		expect(cancel).toHaveBeenCalled();
	});

	it.each(['', '   '])('reports an empty response', async (source) => {
		respond(new Response(source));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('empty');
	});

	it('reports missing response streams and network errors', async () => {
		respond(new Response(null));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('empty');
		fetchMock.mockRejectedValueOnce(new TypeError('Network failed'));
		await expect(loadHtmlPreview(href, new AbortController().signal)).rejects.toThrow('Network failed');
	});
});

describe('HTML preview sandbox document', () => {
	it('requires an opaque-origin scripts-only sandbox', () => {
		expect(HTML_PREVIEW_SANDBOX).toBe('allow-scripts');
		expect(HTML_PREVIEW_SANDBOX).not.toContain('allow-same-origin');
	});

	it('places restrictive CSP before any source, without stripping inline scripts/styles', () => {
		const source = '<!doctype html><html lang="en"><head><style>body { color: red }</style></head><body class="demo"><script>window.test = true</script></body></html>';
		const srcdoc = buildHtmlPreviewDocument(source);
		expect(srcdoc.endsWith(source)).toBe(true);
		const doc = new DOMParser().parseFromString(srcdoc, 'text/html');
		const csp = doc.querySelector('meta[http-equiv="Content-Security-Policy"]')!;
		for (const rule of ["default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'", 'img-src data: blob:', "connect-src 'none'", "form-action 'none'", "frame-src 'none'", "base-uri 'none'", "navigate-to 'none'"]) {
			expect(csp.getAttribute('content')).toContain(rule);
		}
		expect(srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(srcdoc.indexOf('<script>'));
		expect(doc.documentElement.lang).toBe('en');
		expect(doc.body.className).toBe('demo');
		expect([...doc.querySelectorAll('style')].at(-1)?.textContent).toContain('color: red');
	});

	it('injects CSP, theme, base styles and the bootstrap in that order, all before the page', () => {
		const source = '<!doctype html><html><head><style>:root{--background:#000}</style></head><body><h1>Page</h1></body></html>';
		const srcdoc = buildHtmlPreviewDocument(source, { theme });
		const order = ['Content-Security-Policy', 'id="ais-host-theme"', 'id="ais-host-base"', 'id="ais-host-mode"', '<script>', source];
		const positions = order.map((marker) => srcdoc.indexOf(marker));
		expect(positions.every((position) => position >= 0)).toBe(true);
		expect([...positions].sort((a, b) => a - b)).toEqual(positions);
		const doc = new DOMParser().parseFromString(srcdoc, 'text/html');
		const styles = [...doc.querySelectorAll('style')].map((style) => style.id || 'page');
		expect(styles).toEqual(['ais-host-theme', 'ais-host-base', 'ais-host-mode', 'page']);
		// Zero-specificity, so the page's own :root and html rules always win.
		expect(doc.getElementById('ais-host-theme')!.textContent).toMatch(/^:where\(:root\)\{color-scheme:dark;--background:oklch\(0\.1 0 0\);/);
		expect(doc.getElementById('ais-host-base')!.textContent).toContain(':where(html){background:transparent');
		expect(doc.getElementById('ais-host-mode')!.textContent).toContain('overflow-y:hidden');
	});

	it('lets full-screen views scroll and works without a theme snapshot', () => {
		const doc = new DOMParser().parseFromString(buildHtmlPreviewDocument('<p>x</p>', { display: 'fullscreen' }), 'text/html');
		expect(doc.getElementById('ais-host-mode')!.textContent).toBe('');
		expect(doc.getElementById('ais-host-theme')!.textContent).toBe('');
	});

	it('cannot be broken out of by a hostile theme value', () => {
		const srcdoc = buildHtmlPreviewDocument('<p>x</p>', {
			theme: { appearance: 'light', variables: { '--primary': 'red;}</style><script>alert(1)</script>', 'bad name': 'x', '--ok': 'blue' } }
		});
		const doc = new DOMParser().parseFromString(srcdoc, 'text/html');
		const css = doc.getElementById('ais-host-theme')!.textContent!;
		expect(css).not.toMatch(/[<>]/);
		expect(css).toContain('color-scheme:light;');
		expect(css).toContain('--ok:blue;');
		expect(css).not.toContain('bad name');
		expect(doc.querySelectorAll('script')).toHaveLength(1);
	});

	it('builds the live theme message', () => {
		expect(hostContextMessage(theme, true)).toEqual({
			jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.hostContextChanged,
			params: { theme: 'dark', styles: { variables: theme.variables }, scrollable: true }
		});
		expect('scrollable' in hostContextMessage(theme).params).toBe(false);
	});

	it('keeps its policy first even when source attempts to supply a permissive policy', () => {
		const source = '<meta http-equiv="Content-Security-Policy" content="default-src *"><script>malicious()</script>';
		const doc = new DOMParser().parseFromString(buildHtmlPreviewDocument(source), 'text/html');
		expect(doc.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content')).toContain("default-src 'none'");
		expect(doc.querySelector('meta[name="referrer"]')?.getAttribute('content')).toBe('no-referrer');
	});
});

describe('HTML preview frame messages', () => {
	const size = (height: unknown) => ({ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.sizeChanged, params: { height } });
	const link = (url: unknown) => ({ jsonrpc: '2.0', id: 'l1', method: HTML_PREVIEW_METHODS.openLink, params: { url } });

	it('reads exactly the known shapes', () => {
		expect(readHtmlPreviewMessage(size(420.5))).toEqual({ kind: 'size', height: 420.5 });
		expect(readHtmlPreviewMessage({ ...size(400), params: { height: 400, viewport: 320 } })).toEqual({ kind: 'size', height: 400, viewport: 320 });
		expect(readHtmlPreviewMessage({ ...size(400), params: { height: 400, viewport: 'x' } })).toEqual({ kind: 'size', height: 400 });
		expect(readHtmlPreviewMessage(link('https://example.com/a?b#c'))).toEqual({ kind: 'link', url: 'https://example.com/a?b#c' });
		expect(readHtmlPreviewMessage({ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.escape })).toEqual({ kind: 'escape' });
		expect(readHtmlPreviewMessage({ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.openImage, params: { src: 'data:image/png;base64,AAAA', name: ' Chart ' } }))
			.toEqual({ kind: 'image', src: 'data:image/png;base64,AAAA', name: 'Chart' });
	});

	it('rejects malformed, unknown and unsafe requests', () => {
		for (const data of [
			null, 'size', 42, { method: HTML_PREVIEW_METHODS.sizeChanged, params: { height: 1 } },
			{ jsonrpc: '2.0', method: 'ui/unknown' }, size(-1), size(Number.NaN), size('300'), size(Infinity),
			link('javascript:alert(1)'), link('data:text/html,hi'), link('/relative'), link('https://user:pw@example.com'),
			link('https://exa\nmple.com'), link(42), link(`https://example.com/${'a'.repeat(9000)}`),
			{ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.openImage, params: { src: 'https://example.com/x.png' } },
			{ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.openImage, params: { src: 'data:text/html,<script>' } },
			{ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.hostContextChanged, params: {} }
		]) {
			expect(readHtmlPreviewMessage(data)).toBeNull();
		}
	});

	it('accepts messages only from the frame itself, with the opaque origin', () => {
		const frame = document.createElement('iframe');
		document.body.append(frame);
		const own = frame.contentWindow!;
		const event = (source: unknown, origin: string) => ({ source, origin, data: size(300) }) as unknown as MessageEvent;
		expect(acceptHtmlPreviewMessage(event(own, 'null'), frame)).toEqual({ kind: 'size', height: 300 });
		expect(acceptHtmlPreviewMessage(event(window, 'null'), frame)).toBeNull();
		expect(acceptHtmlPreviewMessage(event(null, 'null'), frame)).toBeNull();
		expect(acceptHtmlPreviewMessage(event(own, 'https://app.example'), frame)).toBeNull();
		expect(acceptHtmlPreviewMessage(event(own, 'null'), null)).toBeNull();
		frame.remove();
	});

	it('gates links on frame focus and a live user activation', () => {
		const frame = document.createElement('iframe');
		const focused = { activeElement: frame } as unknown as Document;
		const elsewhere = { activeElement: document.body } as unknown as Document;
		const active = { userActivation: { isActive: true } } as unknown as Navigator;
		const stale = { userActivation: { isActive: false } } as unknown as Navigator;
		const unsupported = {} as Navigator;
		expect(previewHasUserIntent(frame, focused, active)).toBe(true);
		expect(previewHasUserIntent(frame, focused, stale)).toBe(false);
		expect(previewHasUserIntent(frame, elsewhere, active)).toBe(false);
		expect(previewHasUserIntent(null, focused, active)).toBe(false);
		// Without the API, focus alone (as in browsers that predate it).
		expect(previewHasUserIntent(frame, focused, unsupported)).toBe(true);
	});

	it('only lets http(s) URLs leave the frame', () => {
		expect(safeExternalUrl('https://example.com')).toBe('https://example.com/');
		expect(safeExternalUrl('HTTP://Example.com/x')).toBe('http://example.com/x');
		for (const url of ['javascript:void(0)', 'file:///etc/passwd', 'blob:null/1', 'about:blank', 'mailto:a@b.c', 'not a url']) {
			expect(safeExternalUrl(url)).toBeNull();
		}
	});
});

describe('HTML preview fit to content', () => {
	const sizer = (initial = {}) => new HtmlPreviewSizer(initial);

	it('fits the first report and keeps following content changes, clamped', () => {
		const s = sizer();
		expect(s.mode).toBe('measuring');
		expect(s.height).toBe(HTML_PREVIEW_INITIAL_HEIGHT);
		expect(s.settled).toBe(false);
		expect(s.report(480.2)).toBe('resize');
		expect([s.mode, s.height, s.settled]).toEqual(['fit', 481, true]);
		expect(s.report(200)).toBe('resize');
		expect(s.height).toBe(200);
		expect(s.report(200.4)).toBe('none');
		s.report(3);
		expect(s.height).toBe(HTML_PREVIEW_MIN_HEIGHT);
	});

	it('pins a page that is exactly as tall as its frame (100vh) once a probe confirms it', () => {
		const s = sizer();
		expect(s.report(HTML_PREVIEW_INITIAL_HEIGHT)).toBe('probe');
		expect(s.mode).toBe('probing');
		expect(s.height).toBe(HTML_PREVIEW_INITIAL_HEIGHT + HTML_PREVIEW_PROBE_STEP);
		expect(s.report(HTML_PREVIEW_INITIAL_HEIGHT + HTML_PREVIEW_PROBE_STEP)).toBe('fixed');
		expect([s.mode, s.height]).toEqual(['fixed', HTML_PREVIEW_FALLBACK_HEIGHT]);
		expect(s.report(9000)).toBe('none');
	});

	it('fits a page that only happens to match the loading height', () => {
		const s = sizer();
		expect(s.report(HTML_PREVIEW_INITIAL_HEIGHT)).toBe('probe');
		// Content-sized pages do not report when only the frame grows.
		expect(s.probeExpired()).toBe('resize');
		expect([s.mode, s.height]).toEqual(['fit', HTML_PREVIEW_INITIAL_HEIGHT]);
		expect(s.probeExpired()).toBe('none');
	});

	it('detects runaway growth from min-height:100vh plus padding and falls back', () => {
		const s = sizer();
		let reported = HTML_PREVIEW_INITIAL_HEIGHT + 40;
		let action = s.report(reported);
		let steps = 0;
		while (action === 'resize' && steps++ < 50) {
			reported = s.height + 40; // the page is always 40px taller than its frame
			action = s.report(reported);
		}
		expect(action).toBe('probe');
		expect(steps).toBeLessThanOrEqual(3);
		expect(s.report(s.height + 40)).toBe('fixed');
		expect(s.height).toBe(HTML_PREVIEW_FALLBACK_HEIGHT);
	});

	it('keeps fitting content that grows on its own (a chart animating in)', () => {
		const s = sizer();
		s.report(200);
		const frames = [230, 262, 290, 312, 330, 344, 354, 360];
		for (const height of frames) {
			const action = s.report(height);
			if (action === 'probe') {
				// The page does not follow the probe: it keeps its own pace.
				expect(s.report(height + 3)).toBe('resize');
			}
		}
		expect(s.mode).toBe('fit');
		expect(s.height).toBeGreaterThanOrEqual(360);
	});

	it('ignores zero heights and probe answers measured before the probe took effect', () => {
		const s = sizer();
		expect(s.report(0)).toBe('none');
		expect(s.mode).toBe('measuring');
		expect(s.report(320, 320)).toBe('probe');
		// A lagging report measured in the old 320px frame is not the answer.
		expect(s.report(368, 320)).toBe('none');
		expect(s.mode).toBe('probing');
		// Measured in the probe frame, and it moved with it: pinned.
		expect(s.report(s.height, s.height)).toBe('fixed');
	});

	it('catches a throttled follower whose reports lag behind the frame', () => {
		const s = sizer();
		// min-height:100vh plus 48px of padding; each report measured in the previous frame.
		let viewport = HTML_PREVIEW_INITIAL_HEIGHT;
		let action = s.report(viewport + 48, viewport);
		for (let step = 0; action === 'resize' && step < 20; step++) {
			viewport = s.height;
			action = s.report(viewport + 48, viewport);
		}
		expect(action).toBe('probe');
		expect(s.report(s.height + 48, s.height)).toBe('fixed');
	});

	it('ignores reports measured before layout or in a frame size that has since changed', () => {
		const s = sizer();
		// Laid out with no viewport yet: not a measurement.
		expect(s.report(192, 0)).toBe('none');
		expect(s.mode).toBe('measuring');
		expect(s.report(368, 320)).toBe('resize');
		expect(s.height).toBe(368);
		// Measured in the old 320px frame after the resize to 368: stale.
		expect(s.report(240, 320)).toBe('none');
		expect(s.height).toBe(368);
		// A content page re-measured in the new frame keeps its fit.
		expect(s.report(368, 368)).toBe('none');
		expect(s.report(300, 368)).toBe('resize');
		expect(s.height).toBe(300);
	});

	it('pins a follower even when stale reports interleave with fresh ones', () => {
		const s = sizer();
		let previous = HTML_PREVIEW_INITIAL_HEIGHT;
		let action = s.report(previous + 48, previous);
		for (let step = 0; action === 'resize' && step < 20; step++) {
			// A late report from the frame size before last, then the fresh one.
			expect(s.report(previous + 48, previous)).toBe('none');
			previous = s.height;
			action = s.report(previous + 48, previous);
		}
		expect(action).toBe('probe');
		expect(s.report(s.height + 48, s.height)).toBe('fixed');
	});

	it('pins a page that shrinks with its frame (calc(100vh - 16px)) instead of collapsing it', () => {
		const s = sizer();
		let viewport = HTML_PREVIEW_INITIAL_HEIGHT;
		let action = s.report(viewport - 16, viewport);
		let trips = 1;
		for (; action === 'resize' && trips < 20; trips++) {
			viewport = s.height;
			action = s.report(viewport - 16, viewport);
		}
		expect(action).toBe('probe');
		expect(trips).toBeLessThanOrEqual(4);
		expect(s.steady).toBe(false);
		expect(s.height).toBeGreaterThan(HTML_PREVIEW_INITIAL_HEIGHT - 16 * 4);
		expect(s.report(s.height - 16, s.height)).toBe('fixed');
		expect([s.mode, s.height, s.steady]).toEqual(['fixed', HTML_PREVIEW_FALLBACK_HEIGHT, true]);
	});

	it('also catches the shrinking follower from a remembered tall size', () => {
		const s = sizer({ height: 900 });
		let action = s.report(884, 900);
		for (let step = 0; action === 'resize' && step < 20; step++) action = s.report(s.height - 16, s.height);
		expect(action).toBe('probe');
		expect(s.report(s.height - 16, s.height)).toBe('fixed');
	});

	it('keeps fitting a content page across the resize report that follows each fit', () => {
		const s = sizer();
		expect(s.report(500, 320)).toBe('resize');
		expect(s.report(500, 500)).toBe('none');
		expect(s.steady).toBe(true);
		expect(s.report(420, 500)).toBe('resize');
		expect(s.report(420, 420)).toBe('none');
		expect([s.mode, s.height]).toEqual(['fit', 420]);
	});

	it('re-probes a remembered pin instead of trusting it', () => {
		// The page still follows its frame: pinned again after one probe.
		const legacy = sizer({ height: 610, fixed: true });
		expect([legacy.mode, legacy.height, legacy.settled]).toEqual(['measuring', HTML_PREVIEW_FALLBACK_HEIGHT, false]);
		expect(legacy.report(HTML_PREVIEW_FALLBACK_HEIGHT + 48, HTML_PREVIEW_FALLBACK_HEIGHT)).toBe('probe');
		expect(legacy.report(legacy.height + 48, legacy.height)).toBe('fixed');
		// Revised at the same path into a content-sized page: it fits.
		const revised = sizer({ height: 610, fixed: true });
		expect(revised.report(300, HTML_PREVIEW_FALLBACK_HEIGHT)).toBe('probe');
		expect(revised.report(300, revised.height)).toBe('resize');
		expect([revised.mode, revised.height]).toEqual(['fit', 300]);
	});

	it('reserves a height while measuring a page from scratch', () => {
		const s = sizer({ reserve: 700 });
		expect([s.mode, s.height, s.settled]).toEqual(['measuring', 700, false]);
		expect(s.report(450, 700)).toBe('resize');
		expect(s.height).toBe(450);
	});

	it('grows to the full height of a long report instead of scrolling inside', () => {
		// A real nightly report: 41k px on desktop, 82k px on a phone.
		for (const tall of [41_012, 81_686]) {
			const s = sizer();
			expect(s.report(tall, s.height)).toBe('probe');
			expect(s.report(tall, s.height)).toBe('resize');
			expect(s.report(tall, s.height)).toBe('none');
			expect([s.mode, s.height]).toEqual(['fit', tall]);
		}
	});

	// Drive the sizer the way the frame does: every resize gets a fresh report
	// from a page whose height is `page(viewport)`. With `expire`, every probe
	// times out first (a throttled offscreen frame) and its answer comes late.
	// Returns the tallest frame.
	function drive(s: HtmlPreviewSizer, page: (viewport: number) => number, expire = false) {
		let peak = s.height;
		for (let i = 0; i < 40 && s.mode !== 'fixed'; i++) {
			const viewport = s.height;
			let action = s.report(page(viewport), viewport);
			peak = Math.max(peak, s.height);
			if (action === 'probe' && expire) {
				action = s.probeExpired();
				peak = Math.max(peak, s.height);
			}
			if (action === 'none') break;
		}
		return peak;
	}

	const followers: [string, (v: number) => number][] = [
		['37 sections', (v) => 37 * v], ['20 sections', (v) => 20 * v], ['10 sections', (v) => 10 * v],
		['5 sections', (v) => 5 * v], ['2 sections', (v) => 2 * v], ['1.5 × vh', (v) => 1.5 * v],
		['1.2 × vh', (v) => 1.2 * v], ['vh + 40k', (v) => v + 40_000], ['vh + 2k', (v) => v + 2_000]
	];

	it('pins pages built from viewport-height sections without ballooning the frame', () => {
		for (const expire of [false, true]) {
			for (const [name, page] of followers) {
				const s = sizer();
				const peak = drive(s, page, expire);
				expect(s.mode, `${name}, expire=${expire}`).toBe('fixed');
				expect(peak, `${name}, expire=${expire}`).toBeLessThanOrEqual(2 * HTML_PREVIEW_TALL_HEIGHT + HTML_PREVIEW_PROBE_STEP);
			}
		}
		// From a remembered height, a probe grows the frame at most twice over.
		const remembered = sizer({ height: 20_000 });
		expect(drive(remembered, (v) => 10 * v, true)).toBeLessThanOrEqual(40_000 + HTML_PREVIEW_PROBE_STEP);
		expect(remembered.mode).toBe('fixed');
	});

	it('reaches the full height of a long report even when every probe is answered late', () => {
		for (const tall of [41_012, 97_144]) {
			const s = sizer();
			drive(s, () => tall, true);
			expect([s.mode, s.height]).toEqual(['fit', tall]);
		}
	});

	it('fits a long report that grows after it is shown, without probing or pinning', () => {
		const s = sizer({ height: 41_012 });
		// A section opening, then late images arriving in one report.
		for (const next of [41_812, 42_412, 43_612, 44_412]) {
			expect(s.report(next, s.height)).toBe('resize');
			expect(s.report(next, s.height)).toBe('none');
			expect([s.mode, s.height]).toEqual(['fit', next]);
		}
		// Growing past a stale frame several times (offscreen): never pinned.
		for (const next of [45_000, 46_000, 47_000]) s.report(next, s.height);
		expect(s.mode).not.toBe('fixed');
	});

	it('probes a long report once and settles at its height', () => {
		const s = sizer();
		expect(s.report(41_012, s.height)).toBe('probe');
		expect(s.height).toBe(HTML_PREVIEW_TALL_HEIGHT + HTML_PREVIEW_PROBE_STEP);
		expect(drive(s, () => 41_012)).toBe(41_012);
		expect([s.mode, s.height]).toEqual(['fit', 41_012]);
	});

	it('pins rather than clips when an unanswered probe was past the ceiling', () => {
		const s = sizer();
		expect(s.report(HTML_PREVIEW_MAX_HEIGHT + 500, s.height)).toBe('probe');
		expect(s.height).toBe(HTML_PREVIEW_TALL_HEIGHT + HTML_PREVIEW_PROBE_STEP);
		expect(s.report(HTML_PREVIEW_MAX_HEIGHT + 500, s.height)).toBe('fixed');
		// A probe that times out keeps the frame where it is; the late answer
		// still decides, so the page is never fitted at the ceiling.
		const probing = sizer({ height: 400 });
		expect(probing.report(HTML_PREVIEW_MAX_HEIGHT + 500, 400)).toBe('probe');
		const probeFrame = probing.height;
		probing.probeExpired();
		expect([probing.mode, probing.height, probing.steady]).toEqual(['fit', probeFrame, false]);
		expect(probing.report(HTML_PREVIEW_MAX_HEIGHT + 500, probeFrame)).toBe('fixed');
		expect(probing.height).toBe(HTML_PREVIEW_FALLBACK_HEIGHT);
	});

	it('holds a tall page at the probe size, scrollable, until its late answer decides', () => {
		const s = sizer();
		s.report(41_012, s.height);
		const probeFrame = s.height;
		expect(s.probeExpired()).toBe('resize');
		// Nothing out of reach while waiting, and nothing remembered.
		expect([s.mode, s.height, s.scrollable, s.steady, s.settled]).toEqual(['fit', probeFrame, true, false, true]);
		// A stale report (measured before the probe) changes nothing.
		expect(s.report(41_012, HTML_PREVIEW_INITIAL_HEIGHT)).toBe('none');
		expect(s.scrollable).toBe(true);
		// The late answer: a long report fits its full height and stops scrolling.
		expect(s.report(41_012, probeFrame)).toBe('resize');
		expect([s.mode, s.height, s.scrollable, s.steady]).toEqual(['fit', 41_012, false, true]);
	});

	it('keeps a late-answer pin pinned', () => {
		const s = sizer({ height: 11_840 });
		expect(s.report(37 * 11_840, 11_840)).toBe('probe');
		s.probeExpired();
		expect(s.report(37 * s.height, s.height)).toBe('fixed');
		expect([s.mode, s.height, s.scrollable, s.steady]).toEqual(['fixed', HTML_PREVIEW_FALLBACK_HEIGHT, true, true]);
		// Nothing left waiting: later reports and timers change nothing.
		expect(s.probeExpired()).toBe('none');
		expect(s.report(1_000_000, HTML_PREVIEW_FALLBACK_HEIGHT)).toBe('none');
		expect(s.height).toBe(HTML_PREVIEW_FALLBACK_HEIGHT);
	});

	it('re-measures a page pinned by an older build at its full height, not clipped', () => {
		const s = sizer({ height: HTML_PREVIEW_FALLBACK_HEIGHT, fixed: true });
		expect(s.report(41_012, HTML_PREVIEW_FALLBACK_HEIGHT)).toBe('probe');
		expect(s.report(41_012, s.height)).toBe('resize');
		expect(s.report(41_012, s.height)).toBe('none');
		expect([s.mode, s.height]).toEqual(['fit', 41_012]);
	});

	it('falls back when a page reports more than the maximum height', () => {
		const s = sizer();
		s.report(400);
		expect(s.report(HTML_PREVIEW_MAX_HEIGHT + 500)).toBe('probe');
		expect(s.report(HTML_PREVIEW_MAX_HEIGHT + 500)).toBe('fixed');
		expect(s.height).toBe(HTML_PREVIEW_FALLBACK_HEIGHT);
	});

	it('starts from a remembered size and gives up on a page that never reports', () => {
		expect(sizer({ height: 610 })).toMatchObject({ mode: 'fit', height: 610 });
		const silent = sizer();
		expect(silent.giveUp()).toBe('fixed');
		expect(silent.mode).toBe('fixed');
	});
});

describe('HTML preview placement in the reply', () => {
	const preview = (path: string) => ({ href: `/api/files/by-path?path=${encodeURIComponent(path)}`, path, filename: path.split('/').pop()! });
	const a = preview('/work/a.html');
	const b = preview('/work/b.html');
	const routed = (path: string) => `https://worker.example/api/files/by-path?path=${encodeURIComponent(path)}&amp;session_id=s1`;
	const same = (html: string) => html;

	it('sanitizes every markup piece again after splitting', () => {
		const sanitize = vi.fn((html: string) => `[clean]${html}`);
		const html = `<p>See <a href="${a.href}">A</a></p><p>End</p>`;
		const segments = placeHtmlPreviews(html, [a], sanitize);
		expect(sanitize.mock.calls.map(([piece]) => piece)).toEqual([`<p>See <a href="${a.href}">A</a></p>`, '<p>End</p>']);
		expect(segments.filter((s) => s.kind === 'html').every((s) => s.kind === 'html' && s.html.startsWith('[clean]'))).toBe(true);
	});

	it('puts each preview right after the top-level block that links to it', () => {
		const html = `<p>Intro</p><p>See <a href="${routed(a.path)}">A</a> here.</p><ul><li><a href="${routed(b.path)}">B</a></li></ul><p>End</p>`;
		const segments = placeHtmlPreviews(html, [a, b], same);
		expect(segments.map((s) => s.kind === 'html' ? s.html : `[${s.preview.filename}]`)).toEqual([
			`<p>Intro</p><p>See <a href="${routed(a.path)}">A</a> here.</p>`,
			'[a.html]',
			`<ul><li><a href="${routed(b.path)}">B</a></li></ul>`,
			'[b.html]',
			'<p>End</p>'
		]);
		expect(new Set(segments.map((s) => s.key)).size).toBe(segments.length);
	});

	it('keeps two previews linked from one paragraph in order and appends unmatched ones', () => {
		const c = preview('/work/c.html');
		const html = `<p><a href="${b.href}">B</a> and <a href="${a.href}">A</a></p>\n<p>tail &amp; &lt;escaped&gt;</p>`;
		const segments = placeHtmlPreviews(html, [a, b, c], same);
		expect(segments.map((s) => s.kind === 'html' ? 'html' : s.preview.filename)).toEqual(['html', 'b.html', 'a.html', 'html', 'c.html']);
		expect((segments[3] as { html: string }).html).toBe('\n<p>tail &amp; &lt;escaped&gt;</p>');
	});

	it('returns the markup untouched when there is nothing to place', () => {
		expect(placeHtmlPreviews('<p>x</p>', [], same)).toEqual([{ kind: 'html', key: 'h0', html: '<p>x</p>' }]);
	});

	it('names a preview link while the reply is still streaming', () => {
		expect(streamingPreviewName('Here: [Dash](/api/files/by-path?path=%2Fwork%2Fdash%20v2.html) ok')).toBe('dash v2.html');
		expect(streamingPreviewName('Here: [Dash](/api/files/by-path?path=%2Fwork%2Fdash.ht')).toBeNull();
		expect(streamingPreviewName('[doc](/api/files/by-path?path=%2Fwork%2Fdoc.pdf)')).toBeNull();
	});
});

