import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import ChangedFilesPanel from './ChangedFilesPanel.svelte';

const gitMock = vi.hoisted(() => ({
	stageFiles: vi.fn(), unstageFiles: vi.fn(), discardChanges: vi.fn(), refreshChangedFiles: vi.fn()
}));
const files = vi.hoisted(() => ({ list: [] as unknown[] }));
vi.mock('$lib/stores/git.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { git: gitMock, changedFiles: writable(files.list), changedFilesLoading: writable(false) };
});
const notifyMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/notifications', () => ({ notify: notifyMock }));
vi.mock('$lib/stores/platform.svelte', () => ({ platform: { isMobile: false } }));
afterEach(cleanup);
describe('embedded changes panel', () => {
	it('does not create a backdrop, private resize handle, or duplicate close button', () => {
		const { container } = render(ChangedFilesPanel, { projectId: 'test', open: true, reviewMode: true,
			embedded: true, onClose: vi.fn(), onViewDiff: vi.fn() });
		expect(container.querySelector('.panel-backdrop')).toBeNull();
		expect(container.querySelector('.resize-handle')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
		expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBeNull();
	});
	it('closes with Escape without consuming chat arrow keys', async () => {
		const onClose = vi.fn();
		render(ChangedFilesPanel, { projectId: 'test', open: true, reviewMode: true,
			embedded: true, onClose, onViewDiff: vi.fn() });
		const event = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
		window.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(false);
		await fireEvent.keyDown(window, { key: 'Escape' });
		// Docked, it decides once every listener has seen the Escape.
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(onClose).toHaveBeenCalledOnce();
	});
	it('stays open when a popover attached after it claims the Escape', async () => {
		const onClose = vi.fn();
		render(ChangedFilesPanel, { projectId: 'test', open: true, reviewMode: true,
			embedded: true, onClose, onViewDiff: vi.fn() });
		const popover = (e: KeyboardEvent) => { if (e.key === 'Escape') e.preventDefault(); };
		window.addEventListener('keydown', popover);
		try {
			await fireEvent.keyDown(window, { key: 'Escape' });
			await new Promise((resolve) => setTimeout(resolve, 0));
			expect(onClose).not.toHaveBeenCalled();
		} finally {
			window.removeEventListener('keydown', popover);
		}
	});
	it('preserves the standalone close button and modal layout', async () => {
		const onClose = vi.fn();
		const { container } = render(ChangedFilesPanel, { projectId: 'test', open: true,
			onClose, onViewDiff: vi.fn() });
		expect(container.querySelector('.panel-backdrop')).not.toBeNull();
		expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		expect(onClose).toHaveBeenCalledOnce();
	});
});

describe('changes panel action failures', () => {
	const locked = { status: 500, detail: "fatal: Unable to create '.git/index.lock': File exists." };
	afterEach(() => { files.list.length = 0; vi.clearAllMocks(); });

	it('shows a refused stage in the panel, keeps a bell entry, and clears on Dismiss', async () => {
		files.list.push({ file: 'src/a.ts', category: 'modified', status: 'M', additions: 1, deletions: 0, is_binary: false });
		gitMock.stageFiles.mockRejectedValueOnce(Object.assign(new Error(locked.detail), locked));
		render(ChangedFilesPanel, { projectId: 'test', open: true, onClose: vi.fn(), onViewDiff: vi.fn() });
		await fireEvent.click(screen.getByTitle('Stage'));
		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain("Couldn't stage the file");
		expect(alert.textContent).toContain('index.lock');
		expect(notifyMock).toHaveBeenCalledWith(expect.stringContaining('index.lock'), { severity: 'error' });
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss error' }));
		expect(screen.queryByRole('alert')).toBeNull();
	});
});
