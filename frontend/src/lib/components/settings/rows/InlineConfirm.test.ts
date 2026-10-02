import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import InlineConfirm from './InlineConfirm.svelte';
import SettingRowHarness from '$lib/test-stubs/SettingRowHarness.svelte';

afterEach(cleanup);

describe('InlineConfirm', () => {
	it('swaps the trigger for the question, focusing Cancel', async () => {
		render(InlineConfirm, { label: 'Remove', question: 'Remove this key?', onConfirm: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		expect(screen.getByText('Remove this key?')).toBeTruthy();
		const cancel = screen.getByRole('button', { name: 'Cancel' });
		await waitFor(() => expect(document.activeElement).toBe(cancel));
		expect(screen.getByRole('button', { name: 'Remove' })).toBeTruthy();
	});

	it('confirms once and returns to the trigger', async () => {
		const onConfirm = vi.fn();
		render(InlineConfirm, { label: 'Revoke', question: 'Revoke access?', confirmLabel: 'Revoke now', onConfirm });
		await fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Revoke now' }));
		expect(onConfirm).toHaveBeenCalledOnce();
		await waitFor(() => expect(screen.queryByText('Revoke access?')).toBeNull());
		expect(screen.getByRole('button', { name: 'Revoke' })).toBeTruthy();
	});

	it('cancels with Esc and gives focus back to the trigger', async () => {
		const onConfirm = vi.fn();
		const onCancel = vi.fn();
		render(InlineConfirm, { label: 'Sign out', question: 'Sign out here?', onConfirm, onCancel });
		await fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
		await fireEvent.keyDown(screen.getByRole('button', { name: 'Cancel' }), { key: 'Escape' });
		expect(screen.queryByText('Sign out here?')).toBeNull();
		expect(onConfirm).not.toHaveBeenCalled();
		expect(onCancel).toHaveBeenCalledOnce();
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Sign out' })));
	});

	it('cancels with the Cancel button', async () => {
		render(InlineConfirm, { label: 'Deactivate', question: 'Pause new runs here?', onConfirm: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(screen.queryByText('Pause new runs here?')).toBeNull();
	});

	it('holds the buttons while an async confirm runs', async () => {
		let finish: () => void = () => {};
		const onConfirm = vi.fn(
			() =>
				new Promise<void>((resolve) => {
					finish = resolve;
				})
		);
		render(InlineConfirm, { label: 'Use', question: 'Use this reset?', tone: 'primary', onConfirm });
		await fireEvent.click(screen.getByRole('button', { name: 'Use' }));
		const confirm = screen.getByRole('button', { name: 'Use' }) as HTMLButtonElement;
		expect(confirm.classList.contains('primary')).toBe(true);
		await fireEvent.click(confirm);
		expect(confirm.disabled).toBe(true);
		await fireEvent.click(confirm);
		expect(onConfirm).toHaveBeenCalledOnce();
		finish();
		await waitFor(() => expect(screen.queryByText('Use this reset?')).toBeNull());
	});

	it('is disabled when Settings is read-only', () => {
		render(SettingRowHarness, {
			label: 'Remove',
			readOnly: true,
			confirm: { label: 'Remove', question: 'Remove?', onConfirm: vi.fn() }
		});
		expect((screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement).disabled).toBe(true);
	});
});
