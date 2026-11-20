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
	const root = await screen.findByText('root');
	await fireEvent.click(root.closest('.tree-row')!.querySelector('.expand-btn')!);
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
});

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
