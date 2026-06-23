/**
 * Tests for gitCheckoutAction.ts — the shared "switch branch + surface errors"
 * helper used by BranchSelector and GitHeaderActions.
 *
 * Covers the three behaviors the bug fix introduced:
 *  1. success → optional afterSwitch callback runs, no error toast
 *  2. 409 uncommitted-changes → error toast with a "Stash & switch" action that
 *     re-runs the checkout with auto-stash and then shows a success toast
 *  3. non-409 failure → plain error toast (no stash action)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	checkout: vi.fn(),
	toastPush: vi.fn(),
}));

vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		checkout: mocks.checkout,
	},
}));

vi.mock('$lib/stores/toast', () => ({
	toast: {
		push: mocks.toastPush,
	},
}));

// Use the real isApiError type guard (shape-based), so we just feed it the
// right-shaped objects below.
import { checkoutBranchWithToast } from './gitCheckoutAction';

function apiError(status: number, detail: string) {
	return { status, detail };
}

beforeEach(() => {
	mocks.checkout.mockReset();
	mocks.toastPush.mockReset();
	vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('checkoutBranchWithToast', () => {
	it('switches successfully and runs afterSwitch with no error toast', async () => {
		mocks.checkout.mockResolvedValue({ success: true, message: "Checked out 'feature'" });
		const afterSwitch = vi.fn();

		await checkoutBranchWithToast('feature', afterSwitch);

		expect(mocks.checkout).toHaveBeenCalledWith('feature', false);
		expect(afterSwitch).toHaveBeenCalledTimes(1);
		// No toast on a clean (non-stash) success.
		expect(mocks.toastPush).not.toHaveBeenCalled();
	});

	it('surfaces a non-409 failure as a plain error toast (no stash action)', async () => {
		mocks.checkout.mockRejectedValue(apiError(400, "Failed to checkout 'gone'. It may not exist."));

		await checkoutBranchWithToast('gone');

		expect(mocks.toastPush).toHaveBeenCalledTimes(1);
		const [message, opts] = mocks.toastPush.mock.calls[0];
		expect(message).toContain('Failed to checkout');
		expect(opts.tone).toBe('error');
		expect(opts.action).toBeUndefined();
	});

	it('offers "Stash & switch" on a 409 uncommitted-changes conflict', async () => {
		mocks.checkout.mockRejectedValue(
			apiError(409, 'Cannot checkout: you have uncommitted changes. Commit or stash them first.')
		);

		await checkoutBranchWithToast('feature');

		expect(mocks.toastPush).toHaveBeenCalledTimes(1);
		const [message, opts] = mocks.toastPush.mock.calls[0];
		expect(message).toContain('uncommitted changes');
		expect(opts.tone).toBe('error');
		expect(opts.action).toBeDefined();
		expect(opts.action.label).toBe('Stash & switch');
	});

	it('stash action re-runs checkout with auto-stash and shows a success toast', async () => {
		// First call: 409. Second call (from the action): success.
		mocks.checkout
			.mockRejectedValueOnce(apiError(409, 'Cannot checkout: you have uncommitted changes.'))
			.mockResolvedValueOnce({ success: true, message: "Stashed changes and checked out 'feature'" });
		const afterSwitch = vi.fn();

		await checkoutBranchWithToast('feature', afterSwitch);

		const action = mocks.toastPush.mock.calls[0][1].action;
		mocks.toastPush.mockReset();

		// Click "Stash & switch"
		action.onClick();
		// Let the async runCheckout settle.
		await vi.waitFor(() => expect(mocks.toastPush).toHaveBeenCalled());

		// Second checkout used auto-stash = true.
		expect(mocks.checkout).toHaveBeenLastCalledWith('feature', true);
		expect(afterSwitch).toHaveBeenCalledTimes(1);
		const [message, opts] = mocks.toastPush.mock.calls[0];
		expect(message).toBe('Stashed changes and switched to feature');
		expect(opts.tone).toBe('success');
	});

	it('does not offer stash again if the stash retry itself fails', async () => {
		mocks.checkout
			.mockRejectedValueOnce(apiError(409, 'Cannot checkout: you have uncommitted changes.'))
			.mockRejectedValueOnce(apiError(409, 'Cannot checkout: failed to stash your uncommitted changes.'));

		await checkoutBranchWithToast('feature');
		const action = mocks.toastPush.mock.calls[0][1].action;
		mocks.toastPush.mockReset();

		action.onClick();
		await vi.waitFor(() => expect(mocks.toastPush).toHaveBeenCalled());

		// The retry (autoStash=true) failure must NOT carry another stash action.
		const opts = mocks.toastPush.mock.calls[0][1];
		expect(opts.tone).toBe('error');
		expect(opts.action).toBeUndefined();
	});
});
