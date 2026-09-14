import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HtmlPreviews from './HtmlPreviews.svelte';
import { registerShortcut } from '$lib/services/keyboard';
import { loadHtmlPreview } from '$lib/utils/htmlPreview';

vi.mock('$lib/utils/htmlPreview', async (original) => ({
	...(await original<typeof import('$lib/utils/htmlPreview')>()),
	loadHtmlPreview: vi.fn()
}));
const load = vi.mocked(loadHtmlPreview);
const content = '[Demo](/api/files/by-path?path=%2Fwork%2Fdemo.html)';
vi.stubGlobal('URL', class extends URL {
	static createObjectURL = vi.fn(() => 'blob:preview-download');
	static revokeObjectURL = vi.fn();
});
beforeEach(() => {
	HTMLDialogElement.prototype.showModal = function () { this.open = true; };
	HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('HTML preview cards', () => {
	it('does not fetch or run streaming content or ordinary links', () => {
		const view = render(HtmlPreviews, { content, streaming: true });
		expect(view.container.querySelector('iframe')).toBeNull();
		expect(load).not.toHaveBeenCalled();
	});

	it('renders sandboxed scripts, source as text, mobile width and refreshed files', async () => {
		load.mockResolvedValue('<h1>Demo</h1><script>window.demo = true</script>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		const frame = view.container.querySelector('iframe')!;
		expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
		expect(frame.hasAttribute('src')).toBe(false);
		expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer');
		expect(frame.srcdoc).toContain('Content-Security-Policy');
		await fireEvent.click(view.getByRole('button', { name: 'Mobile' }));
		expect(frame.classList.contains('mobile')).toBe(true);
		await fireEvent.click(view.getByText('Source'));
		expect(view.getByLabelText('HTML source').textContent).toContain('<script>');
		expect(view.container.querySelector('script')).toBeNull();
		await fireEvent.click(view.getByLabelText('Refresh HTML preview'));
		await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
	});

	it('shows errors and retries instead of mounting a broken frame', async () => {
		load.mockRejectedValueOnce(new Error('File not found')).mockResolvedValueOnce('<p>Recovered</p>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain('File not found'));
		expect(view.container.querySelector('iframe')).toBeNull();
		await fireEvent.click(view.getByText('Try again'));
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
	});

	it('expands to a dialog, closes and releases the downloaded source URL', async () => {
		load.mockResolvedValue('<p>Dialog preview</p>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		expect(view.getByLabelText('Download HTML file').getAttribute('href')).toBe('blob:preview-download');
		await fireEvent.click(view.getByLabelText('Expand HTML preview'));
		const dialog = view.getByRole('dialog') as HTMLDialogElement;
		expect(dialog.open).toBe(true);
		expect(dialog.querySelector('iframe')).not.toBeNull();
		expect(view.container.querySelectorAll('iframe')).toHaveLength(1);
		await fireEvent.click(view.getByLabelText('Close HTML preview'));
		expect(dialog.open).toBe(false);
		expect(view.container.querySelectorAll('iframe')).toHaveLength(1);
		view.unmount();
		expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview-download');
	});

	it('switches viewport in the expanded dialog and preserves it after closing', async () => {
		load.mockResolvedValue('<p>Responsive preview</p>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		const dialog = within(view.getByRole('dialog'));
		const frame = dialog.getByTitle('HTML preview: demo.html');
		const mobile = dialog.getByRole('button', { name: 'Mobile' });
		const desktop = dialog.getByRole('button', { name: 'Desktop' });
		expect(mobile.textContent).toContain('Mobile');
		expect(desktop.textContent).toContain('Desktop');
		await fireEvent.click(mobile);
		expect(mobile.getAttribute('aria-pressed')).toBe('true');
		expect(desktop.getAttribute('aria-pressed')).toBe('false');
		expect(frame.classList.contains('mobile')).toBe(true);
		await fireEvent.click(desktop);
		expect(frame.classList.contains('mobile')).toBe(false);
		expect(desktop.getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(mobile);
		await fireEvent.click(dialog.getByRole('button', { name: 'Close HTML preview' }));
		expect(view.container.querySelector('iframe')!.classList.contains('mobile')).toBe(true);
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		expect(within(view.getByRole('dialog')).getByRole('button', { name: 'Mobile' }).getAttribute('aria-pressed')).toBe('true');
	});

	it('leaves Escape to native dialog cancellation instead of the global shortcut', async () => {
		load.mockResolvedValue('<p>Preview</p>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		await fireEvent.click(view.getByRole('button', { name: 'Expand HTML preview' }));
		const dialog = view.getByRole('dialog') as HTMLDialogElement;
		const action = vi.fn();
		const shortcut = registerShortcut({id: 'test-escape', key: 'Escape', description: 'Close app surface', category: 'general', action});
		try {
			const event = new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true});
			within(dialog).getByRole('button', {name: 'Mobile'}).dispatchEvent(event);
			expect(action).not.toHaveBeenCalled();
			expect(event.defaultPrevented).toBe(false);
			// jsdom does not perform the browser's native cancel default.
			dialog.close();
			await waitFor(() => expect(view.container.querySelectorAll('iframe')).toHaveLength(1));
		} finally { shortcut.unregister(); }
	});

	it('labels icon controls and keeps the sandbox explanation available', async () => {
		load.mockResolvedValue('<p>Preview</p>');
		const view = render(HtmlPreviews, { content });
		await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull());
		for (const name of ['Desktop', 'Mobile', 'Refresh HTML preview', 'Expand HTML preview']) {
			expect(view.getByRole('button', { name }).getAttribute('title')).toBeTruthy();
		}
		const info = view.container.querySelector('details')!;
		expect(info.open).toBe(false);
		expect(info.textContent).toContain('External resources and server features are not supported.');
		await fireEvent.click(view.getByRole('button', { name: 'Mobile' }));
		expect(view.getByText('375 px')).toBeTruthy();
		await fireEvent.click(view.getByRole('button', { name: 'Desktop' }));
		expect(view.getByText('Responsive')).toBeTruthy();
	});

	it('aborts pending loads on unmount', () => {
		load.mockImplementation(() => new Promise(() => {}));
		const view = render(HtmlPreviews, { content });
		const signal = load.mock.calls[0][1];
		view.unmount();
		expect(signal.aborted).toBe(true);
	});
});
