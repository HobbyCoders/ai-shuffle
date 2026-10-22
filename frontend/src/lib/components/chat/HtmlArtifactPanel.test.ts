import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import HtmlArtifactPanel from './HtmlArtifactPanel.svelte';
import { HTML_PREVIEW_METHODS, loadHtmlPreview } from '$lib/utils/htmlPreview';
import { htmlArtifactPanel } from '$lib/stores/html-artifact-panel.svelte';
import { sessionPanelKey, sidePanel } from '$lib/stores/side-panel.svelte';

vi.mock('$lib/utils/htmlPreview', async (original) => ({
	...(await original<typeof import('$lib/utils/htmlPreview')>()),
	loadHtmlPreview: vi.fn()
}));
vi.stubGlobal('URL', class extends URL {
	static createObjectURL = vi.fn(() => 'blob:panel-download');
	static revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const href = '/api/files/by-path?path=%2Fwork%2Fpanel.html';
const escape = { jsonrpc: '2.0', method: HTML_PREVIEW_METHODS.escape };
function send(frame: HTMLIFrameElement, data: unknown) {
	window.dispatchEvent(new MessageEvent('message', { data, origin: 'null', source: frame.contentWindow }));
}

describe('HTML artifact side panel', () => {
	it('shows the page full height in the panel viewer, scrollable', async () => {
		vi.mocked(loadHtmlPreview).mockResolvedValue('<h1>Panel</h1>');
		const view = render(HtmlArtifactPanel, { href, filename: 'panel.html', sessionId: 'panel-chat' });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		const frame = view.container.querySelector('iframe')!;
		expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
		const doc = new DOMParser().parseFromString(frame.srcdoc, 'text/html');
		expect(doc.getElementById('ais-host-mode')?.textContent).toBe(''); // not the inline no-scroll mode
		expect(view.getByRole('region', { name: 'HTML artifact: panel.html' })).toBeTruthy();
		expect(vi.mocked(loadHtmlPreview)).toHaveBeenCalledWith(href, expect.any(AbortSignal), 'panel-chat');
	});

	it('closes when the focused page passes Escape on, and only then', async () => {
		vi.mocked(loadHtmlPreview).mockResolvedValue('<p>Esc</p>');
		htmlArtifactPanel.open('panel-chat', { href, filename: 'panel.html' });
		const key = sessionPanelKey('panel-chat');
		expect(sidePanel.forScope(key).active).toBe('artifact');
		const view = render(HtmlArtifactPanel, { href, filename: 'panel.html', sessionId: 'panel-chat' });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		const frame = view.container.querySelector('iframe')!;
		send(frame, escape); // the page does not have focus
		await tick();
		expect(sidePanel.forScope(key).active).toBe('artifact');
		frame.focus();
		send(frame, escape);
		await tick();
		expect(sidePanel.forScope(key).active).toBeNull();
	});
});
