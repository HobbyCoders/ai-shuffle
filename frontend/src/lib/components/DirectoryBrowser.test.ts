import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import DirectoryBrowser from './DirectoryBrowser.svelte';

const entry = (name: string, path: string, has_children = false) => ({
	name, path, has_children, is_dir: true
});
const response = (path: string, entries: ReturnType<typeof entry>[]) => ({
	path, parent: null, entries, is_local_mode: false
});

afterEach(cleanup);

it('bounds rendered siblings, reaches the final page, and keeps it on a same-source refresh', async () => {
	const children = Array.from({ length: 501 }, (_, i) => entry(`folder ${i}`, `/root/${i}`));
	const browse = vi.fn(async (path = '') => path ? response(path, children) : response('', [entry('root', '/root', true)]));
	const onSelect = vi.fn();
	const { container, rerender } = render(DirectoryBrowser, { browse, onSelect });
	await screen.findByText('root');
	await fireEvent.click(screen.getByRole('button', { name: 'Show folders in root' }));
	await screen.findByText('folder 0');
	const rows = () => container.querySelectorAll('.tree-row').length;
	expect(rows()).toBeLessThan(250);
	const next = screen.getByRole<HTMLButtonElement>('button', { name: 'Show next folders' });
	while (!next.disabled) await fireEvent.click(next);
	expect(screen.queryByText('folder 0')).toBeNull();
	expect(rows()).toBeLessThan(250);
	const refreshedBrowse = vi.fn((path?: string) => browse(path));
	await rerender({ browse: refreshedBrowse, onSelect, selectedPath: '/root/500' });
	await fireEvent.click(screen.getByText('folder 500'));
	expect(refreshedBrowse).not.toHaveBeenCalled();
	expect(onSelect).toHaveBeenCalledWith('/root/500');
	await fireEvent.click(screen.getByRole('button', { name: 'Show previous folders' }));
	await screen.findByText('folder 200');
}, 15_000); // 501 jsdom rows and three page clicks: 3-4 s locally, past vitest's 5 s default on CI runners (#1868).

it('uses bounded pages at the root and still surfaces browse failures', async () => {
	const roots = Array.from({ length: 250 }, (_, i) => entry(`drive ${i}`, `/drive-${i}`));
	const { unmount } = render(DirectoryBrowser, { onSelect: vi.fn(), browse: async () => response('', roots) });
	await screen.findByText('drive 0');
	expect(screen.queryByText('drive 249')).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Show next folders' }));
	await screen.findByText('drive 249');
	unmount();
	render(DirectoryBrowser, { onSelect: vi.fn(), browse: async () => { throw Error('Cannot open workspace'); } });
	await waitFor(() => expect(screen.getByText('Cannot open workspace')).toBeTruthy());
});

it('expands from the keyboard with a named expander, separate from selecting', async () => {
	const browse = vi.fn(async (path = '') => path
		? response(path, [entry('child', '/root/child')])
		: response('', [entry('root', '/root', true), entry('leaf', '/leaf')]));
	const onSelect = vi.fn();
	render(DirectoryBrowser, { browse, onSelect });
	const expander = await screen.findByRole('button', { name: 'Show folders in root' });
	// A folder without subfolders offers no expander.
	expect(screen.queryByRole('button', { name: 'Show folders in leaf' })).toBeNull();
	// Sibling buttons: the expander is not nested in the select button and is in the Tab order.
	const select = screen.getByRole('button', { name: 'root' });
	expect(select.contains(expander)).toBe(false);
	expect(expander.tabIndex).toBe(0);
	expect(expander.getAttribute('aria-expanded')).toBe('false');

	// A native button: Enter/Space activate it (jsdom does not synthesize that click).
	expect(expander.tagName).toBe('BUTTON');
	await fireEvent.click(expander);
	await screen.findByText('child');
	expect(expander.getAttribute('aria-expanded')).toBe('true');
	expect(onSelect).not.toHaveBeenCalled();

	// ArrowLeft collapses and ArrowRight reopens from the name button too.
	await fireEvent.keyDown(select, { key: 'ArrowLeft' });
	await waitFor(() => expect(screen.queryByText('child')).toBeNull());
	await fireEvent.keyDown(select, { key: 'ArrowRight' });
	await screen.findByText('child');
	expect(browse).toHaveBeenCalledTimes(2); // root listing + one child load, reused after collapse

	await fireEvent.click(select);
	expect(onSelect).toHaveBeenCalledWith('/root');
});

it('shows a failed folder load with a retry instead of an empty folder', async () => {
	let fail = true;
	const browse = vi.fn(async (path = '') => {
		if (!path) return response('', [entry('root', '/root', true)]);
		if (fail) throw Object.assign(Error('boom'), { detail: 'Permission denied' });
		return response(path, [entry('child', '/root/child')]);
	});
	render(DirectoryBrowser, { browse, onSelect: vi.fn() });
	const expander = await screen.findByRole('button', { name: 'Show folders in root' });
	await fireEvent.click(expander);
	const alert = await screen.findByRole('alert');
	expect(alert.textContent).toContain("Couldn't open root: Permission denied");
	expect(expander.getAttribute('aria-expanded')).toBe('false');

	fail = false;
	await fireEvent.click(screen.getByRole('button', { name: 'Retry opening root' }));
	await screen.findByText('child');
	expect(screen.queryByRole('alert')).toBeNull();
	expect(expander.getAttribute('aria-expanded')).toBe('true');
});
