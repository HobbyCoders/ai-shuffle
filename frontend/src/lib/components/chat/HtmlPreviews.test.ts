import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';
import HtmlPreview from './HtmlPreview.svelte';
import { registerShortcut } from '$lib/services/keyboard';
import { HTML_PREVIEW_FALLBACK_HEIGHT, HTML_PREVIEW_INITIAL_HEIGHT, HTML_PREVIEW_METHODS, loadHtmlPreview } from '$lib/utils/htmlPreview';
import { sessionPanelKey, sidePanel } from '$lib/stores/side-panel.svelte';
import { htmlArtifactPanel } from '$lib/stores/html-artifact-panel.svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';
import PreviewSession from './test-fixtures/PreviewSession.svelte';

vi.mock('$lib/utils/htmlPreview', async (original) => ({
	...(await original<typeof import('$lib/utils/htmlPreview')>()),
	loadHtmlPreview: vi.fn()
}));
const load = vi.mocked(loadHtmlPreview);
const href = '/api/files/by-path?path=%2Fwork%2Fdemo.html';
const content = `[Demo](${href})`;
vi.stubGlobal('URL', class extends URL {
	static createObjectURL = vi.fn(() => 'blob:preview-download');
	static revokeObjectURL = vi.fn();
});
beforeEach(() => {
	HTMLDialogElement.prototype.showModal = function () { this.open = true; };
	HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

let fileCounter = 0;
/** A fresh file per test: fitted sizes are remembered per file. */
function uniqueHref() { return `/api/files/by-path?path=%2Fwork%2Fpage-${++fileCounter}.html`; }

function frameOf(container: HTMLElement) { return container.querySelector('iframe') as HTMLIFrameElement | null; }
function clipOf(container: HTMLElement) { return container.querySelector('.frame-clip') as HTMLElement; }
function send(frame: HTMLIFrameElement, data: unknown, options: { source?: MessageEventSource | null; origin?: string } = {}) {
	window.dispatchEvent(new MessageEvent('message', {
		data,
		origin: options.origin ?? 'null',
		source: options.source === undefined ? frame.contentWindow : options.source
	}));
}
const size = (height: number) => ({ jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.sizeChanged, params: { height } });

describe('HTML artifacts inline in the reply', () => {
	it('renders the page right after the paragraph that links to it, keeping the link', async () => {
		load.mockResolvedValue('<h1>Inline</h1>');
		const view = render(AssistantMessage, {
			content: `Intro paragraph.\n\nHere is the [Demo](${href}) dashboard.\n\nClosing notes.`,
			sessionId: 'chat-1'
		});
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const figure = view.container.querySelector('figure.html-artifact')!;
		expect(figure.previousElementSibling?.textContent).toBe('Here is the Demo dashboard.');
		expect(figure.previousElementSibling?.querySelector('a')?.getAttribute('href')).toContain('by-path');
		expect(figure.nextElementSibling?.textContent).toBe('Closing notes.');
		expect(figure.classList.contains('not-prose')).toBe(true);
		expect(view.container.textContent).not.toContain('Isolated preview');
	});

	it('does not fetch or mount frames while streaming, and holds the place quietly', () => {
		const view = render(AssistantMessage, { content, streaming: true, sessionId: 'chat-1' });
		expect(frameOf(view.container)).toBeNull();
		expect(load).not.toHaveBeenCalled();
		const pending = view.container.querySelector('.html-artifact-pending') as HTMLElement;
		expect(pending.style.height).toBe(`${HTML_PREVIEW_INITIAL_HEIGHT}px`);
		expect(pending.textContent).toContain('demo.html');
	});

	it('keeps the loaded frame when scrolling or composing updates the session store', async () => {
		load.mockResolvedValue('<h1>Stable preview</h1>');
		const session = writable({ sessionId: 'preview-chat', inputText: '', scrollTop: 0 });
		const view = render(PreviewSession, { content, session });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const frame = frameOf(view.container)!;
		const signal = load.mock.calls[0][1];

		for (const update of [{ scrollTop: 300 }, { inputText: 'Hello' }, { scrollTop: 600 }]) {
			session.update((state) => ({ ...state, ...update }));
			await tick();
			expect(frameOf(view.container)).toBe(frame);
			expect(load).toHaveBeenCalledTimes(1);
			expect(signal.aborted).toBe(false);
		}

		session.update((state) => ({ ...state, sessionId: 'other-chat' }));
		await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
		expect(signal.aborted).toBe(true);
		expect(load).toHaveBeenLastCalledWith(href, expect.any(AbortSignal), 'other-chat');
	});

	it('runs the page in an opaque-origin srcdoc sandbox, hidden until it reports its size', async () => {
		load.mockResolvedValue('<h1>Demo</h1><script>window.demo = true</script>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'demo.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const frame = frameOf(view.container)!;
		expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
		expect(frame.hasAttribute('src')).toBe(false);
		expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer');
		expect(frame.srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(frame.srcdoc.indexOf('<h1>Demo</h1>'));
		expect(frame.srcdoc).toContain('id="ais-host-theme"');
		expect(view.container.querySelector('script')).toBeNull();
		// Covered by the loading surface (not hidden: a hidden frame is throttled).
		expect(view.getByRole('status', { name: 'Loading demo.html' })).toBeTruthy();
		expect(clipOf(view.container).style.height).toBe(`${HTML_PREVIEW_INITIAL_HEIGHT}px`);

		send(frame, size(612));
		await tick();
		expect(clipOf(view.container).style.height).toBe('612px');
		expect(view.queryByRole('status')).toBeNull();
	});

	it('ignores size reports from any other window or origin', async () => {
		load.mockResolvedValue('<p>x</p>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'x.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const frame = frameOf(view.container)!;
		send(frame, size(900), { source: window });
		send(frame, size(900), { origin: 'https://evil.example' });
		send(frame, size(900), { source: null });
		await tick();
		expect(clipOf(view.container).style.height).toBe(`${HTML_PREVIEW_INITIAL_HEIGHT}px`);
		expect(view.getByRole('status', { name: 'Loading x.html' })).toBeTruthy();
	});

	it('pins a page that grows with its frame and lets it scroll inside', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		load.mockResolvedValue('<body style="min-height:100vh;background:#000"></body>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'legacy.html', sessionId: 'chat-1' });
		await vi.waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const frame = frameOf(view.container)!;
		const post = vi.spyOn(frame.contentWindow!, 'postMessage');
		// Exactly the frame height, then the probe's height: the page follows its frame.
		send(frame, size(HTML_PREVIEW_INITIAL_HEIGHT));
		await tick();
		send(frame, size(parseInt(clipOf(view.container).style.height)));
		await tick();
		expect(clipOf(view.container).style.height).toBe(`${HTML_PREVIEW_FALLBACK_HEIGHT}px`);
		expect(view.container.querySelector('figure')!.classList.contains('fixed')).toBe(true);
		await waitFor(() => expect(post).toHaveBeenCalledWith(expect.objectContaining({
			method: HTML_PREVIEW_METHODS.hostContextChanged,
			params: expect.objectContaining({ scrollable: true })
		}), '*'));
		// Browsers structured-clone posted data: a reactive proxy would throw.
		for (const [data] of post.mock.calls) expect(() => structuredClone(data)).not.toThrow();
		// Later reports cannot grow it again.
		send(frame, size(5000));
		await tick();
		expect(clipOf(view.container).style.height).toBe(`${HTML_PREVIEW_FALLBACK_HEIGHT}px`);
	});

	it('opens external links only for the focused frame with a live user activation', async () => {
		load.mockResolvedValue('<a href="https://example.com">x</a>');
		const open = vi.spyOn(window, 'open').mockImplementation(() => null);
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'links.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const frame = frameOf(view.container)!;
		const link = { jsonrpc: '2.0', id: 'l1', method: HTML_PREVIEW_METHODS.openLink, params: { url: 'https://example.com/docs' } };
		const activation = { isActive: false };
		Object.defineProperty(navigator, 'userActivation', { configurable: true, get: () => activation });
		try {
			send(frame, link); // not focused
			frame.focus();
			expect(document.activeElement).toBe(frame);
			send(frame, link); // focused, but no user activation (script-driven)
			send(frame, { ...link, params: { url: 'javascript:alert(1)' } });
			expect(open).not.toHaveBeenCalled();
			activation.isActive = true;
			send(frame, link, { source: window }); // right moment, wrong sender
			expect(open).not.toHaveBeenCalled();
			send(frame, link);
			expect(open).toHaveBeenCalledExactlyOnceWith('https://example.com/docs', '_blank', 'noopener,noreferrer');
		} finally {
			delete (navigator as { userActivation?: unknown }).userActivation;
		}
	});

	it('shows errors as one quiet line and retries instead of mounting a broken frame', async () => {
		load.mockRejectedValueOnce(new Error('Could not load this HTML file (HTTP 404).')).mockResolvedValueOnce('<p>Recovered</p>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'gone.html', sessionId: 'chat-1' });
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain('HTTP 404'));
		expect(view.getByRole('alert').textContent).toContain('gone.html');
		expect(frameOf(view.container)).toBeNull();
		await fireEvent.click(view.getByText('Try again'));
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
	});

	it('keeps quiet controls: expand, open beside chat, and source/reload/download behind More', async () => {
		load.mockResolvedValue('<p>Controls</p>');
		const page = uniqueHref();
		const view = render(HtmlPreview, { href: page, filename: 'controls.html', sessionId: 'side-chat' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		const toolbar = view.getByRole('toolbar');
		expect(within(toolbar).queryByLabelText('Download HTML file')).toBeNull();
		await fireEvent.click(within(toolbar).getByRole('button', { name: 'More HTML preview actions' }));
		expect(within(toolbar).getByLabelText('Download HTML file').getAttribute('href')).toBe('blob:preview-download');
		await fireEvent.click(within(toolbar).getByRole('button', { name: 'Reload HTML preview' }));
		await waitFor(() => expect(load).toHaveBeenCalledTimes(2));

		await fireEvent.click(view.getByRole('button', { name: 'Open beside chat' }));
		expect(sidePanel.forScope(sessionPanelKey('side-chat')).active).toBe('artifact');
		expect(htmlArtifactPanel.forSession('side-chat')).toEqual({ href: page, filename: 'controls.html' });
		const close = view.getByRole('button', { name: 'Close side panel preview' });
		expect(close.getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(close);
		expect(sidePanel.forScope(sessionPanelKey('side-chat')).active).toBeNull();
	});

	it('expands to a dialog with viewport, source and zoom, and closes', async () => {
		load.mockResolvedValue('<p>Dialog preview</p><script>1</script>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'demo.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		const dialog = view.getByRole('dialog') as HTMLDialogElement;
		expect(dialog.open).toBe(true);
		const inside = within(dialog);
		const mobile = inside.getByRole('button', { name: 'Mobile' });
		await fireEvent.click(mobile);
		expect(mobile.getAttribute('aria-pressed')).toBe('true');
		expect(dialog.querySelector('.zoom-box')!.classList.contains('mobile')).toBe(true);
		await fireEvent.click(inside.getByRole('button', { name: 'Zoom in' }));
		expect(inside.getByRole('button', { name: 'Reset zoom' }).textContent).toBe('110%');
		await fireEvent.click(inside.getByRole('button', { name: 'Source' }));
		expect(inside.getByLabelText('HTML source').textContent).toContain('<script>1</script>');
		expect(inside.getByRole('button', { name: 'Mobile' }).hasAttribute('disabled')).toBe(true);
		await fireEvent.click(inside.getByRole('button', { name: 'Close HTML preview' }));
		expect(dialog.open).toBe(false);
		expect(view.container.querySelectorAll('iframe')).toHaveLength(1);
	});

	it('opens the source view directly from the inline controls', async () => {
		load.mockResolvedValue('<p>Source</p>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'demo.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'More HTML preview actions' }));
		await fireEvent.click(view.getByRole('button', { name: 'View HTML source' }));
		expect(within(view.getByRole('dialog')).getByLabelText('HTML source').textContent).toBe('<p>Source</p>');
	});

	it('leaves Escape to native dialog cancellation instead of the global shortcut', async () => {
		load.mockResolvedValue('<p>Preview</p>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'demo.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		const dialog = view.getByRole('dialog') as HTMLDialogElement;
		const action = vi.fn();
		const shortcut = registerShortcut({ id: 'test-escape', key: 'Escape', description: 'Close app surface', category: 'general', action });
		try {
			const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
			within(dialog).getByRole('button', { name: 'Mobile' }).dispatchEvent(event);
			expect(action).not.toHaveBeenCalled();
			expect(event.defaultPrevented).toBe(false);
		} finally { shortcut.unregister(); }
	});

	it('closes the dialog when the focused page passes Escape on', async () => {
		load.mockResolvedValue('<p>Preview</p>');
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'demo.html', sessionId: 'chat-1' });
		await waitFor(() => expect(frameOf(view.container)).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		const dialog = view.getByRole('dialog') as HTMLDialogElement;
		const frame = dialog.querySelector('iframe')!;
		const escape = { jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.escape };
		send(frame, escape); // the page cannot close it without focus
		expect(dialog.open).toBe(true);
		frame.focus();
		send(frame, escape);
		expect(dialog.open).toBe(false);
	});

	it('aborts pending loads on unmount', () => {
		load.mockImplementation(() => new Promise(() => {}));
		const view = render(HtmlPreview, { href: uniqueHref(), filename: 'slow.html', sessionId: 'chat-1' });
		const signal = load.mock.calls[0][1];
		view.unmount();
		expect(signal.aborted).toBe(true);
	});
});
