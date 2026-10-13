/**
 * The shortcuts modal (built on UniversalModal) takes focus on open, keeps Tab
 * inside, closes on Escape, and hands focus back to whatever opened it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import KeyboardShortcutsModal from './KeyboardShortcutsModal.svelte';

async function settle() {
	await tick();
	await Promise.resolve();
}

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('KeyboardShortcutsModal focus handling', () => {
	it('focuses the close button on open, closes on Escape, and restores focus to the opener', async () => {
		const opener = document.createElement('button');
		opener.textContent = 'Shortcuts';
		document.body.appendChild(opener);
		opener.focus();

		const onClose = vi.fn();
		const view = render(KeyboardShortcutsModal, { props: { open: true, onClose } });
		await settle();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);

		await view.rerender({ open: false, onClose });
		await settle();
		expect(document.activeElement).toBe(opener);
	});

	it('keeps Shift+Tab inside the dialog instead of escaping to the page behind', async () => {
		render(KeyboardShortcutsModal, { props: { open: true, onClose: vi.fn() } });
		await settle();
		const dialog = screen.getByRole('dialog');
		const close = screen.getByRole('button', { name: 'Close' });
		close.focus();
		const notPrevented = await fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
		expect(notPrevented).toBe(false);
		expect(dialog.contains(document.activeElement)).toBe(true);
	});
});
