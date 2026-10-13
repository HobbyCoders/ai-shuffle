import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	buildHtmlPreviewDocument,
	extractHtmlPreviews,
	HTML_PREVIEW_SANDBOX,
	loadHtmlPreview,
	retryHtmlPreview,
	MAX_HTML_PREVIEW_BYTES
} from './htmlPreview';

const href = '/api/files/by-path?path=%2Fwork%2Fdemo.html';
const link = `[Open demo](${href})`;
const fetchMock = vi.fn();

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
		expect(doc.querySelector('style')?.textContent).toContain('color: red');
	});

	it('keeps its policy first even when source attempts to supply a permissive policy', () => {
		const source = '<meta http-equiv="Content-Security-Policy" content="default-src *"><script>malicious()</script>';
		const doc = new DOMParser().parseFromString(buildHtmlPreviewDocument(source), 'text/html');
		expect(doc.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content')).toContain("default-src 'none'");
		expect(doc.querySelector('meta[name="referrer"]')?.getAttribute('content')).toBe('no-referrer');
	});
});
