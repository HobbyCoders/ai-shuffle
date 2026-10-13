/**
 * Keyboard contract for ConfirmDialog: focus moves in on open, Tab stays
 * inside, Escape cancels, Enter confirms — except when a button (e.g. Cancel)
 * has focus, where Enter is that button's own activation — and focus returns
 * to the opener when the dialog closes.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import ConfirmDialog from './ConfirmDialog.svelte';

async function settle() {
	await tick();
	await Promise.resolve();
}

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('ConfirmDialog keyboard and focus', () => {
	it('moves focus to the confirm action on open and restores it to the opener on close', async () => {
		const opener = document.createElement('button');
		opener.textContent = 'Open';
		document.body.appendChild(opener);
		opener.focus();

		const props = { open: true, message: 'Delete it?', confirmLabel: 'Delete', onConfirm: vi.fn(), onCancel: vi.fn() };
		const view = render(ConfirmDialog, { props });
		await settle();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' }));

		await view.rerender({ ...props, open: false });
		await settle();
		expect(document.activeElement).toBe(opener);
	});

	it('Escape cancels', async () => {
		const onCancel = vi.fn();
		render(ConfirmDialog, { props: { open: true, message: 'Sure?', onConfirm: vi.fn(), onCancel } });
		await settle();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onCancel).toHaveBeenCalledTimes(1);
	});

	it('Enter on the focused Cancel button does not confirm', async () => {
		const onConfirm = vi.fn();
		render(ConfirmDialog, { props: { open: true, message: 'Sure?', onConfirm, onCancel: vi.fn() } });
		await settle();
		const cancel = screen.getAllByRole('button', { name: 'Cancel' }).find((b) => b.textContent?.trim() === 'Cancel')!;
		cancel.focus();
		await fireEvent.keyDown(cancel, { key: 'Enter' });
		expect(onConfirm).not.toHaveBeenCalled();
	});

	it('Enter outside a button still confirms', async () => {
		const onConfirm = vi.fn();
		render(ConfirmDialog, { props: { open: true, message: 'Sure?', onConfirm, onCancel: vi.fn() } });
		await settle();
		await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Enter' });
		expect(onConfirm).toHaveBeenCalledTimes(1);
	});

	it('Tab from the last control wraps to the first inside the dialog', async () => {
		render(ConfirmDialog, { props: { open: true, message: 'Sure?', confirmLabel: 'Yes', onConfirm: vi.fn(), onCancel: vi.fn() } });
		await settle();
		const yes = screen.getByRole('button', { name: 'Yes' });
		yes.focus();
		await fireEvent.keyDown(yes, { key: 'Tab' });
		const cancel = screen.getAllByRole('button', { name: 'Cancel' }).find((b) => b.textContent?.trim() === 'Cancel');
		expect(document.activeElement).toBe(cancel);
	});
});
