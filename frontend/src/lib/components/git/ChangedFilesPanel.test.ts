import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import ChangedFilesPanel from './ChangedFilesPanel.svelte';

vi.mock('$lib/stores/git.svelte', () => ({
	git: {}, changedFiles: writable([]), changedFilesLoading: writable(false)
}));
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
