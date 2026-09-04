/**
 * Shared "switch branch and surface errors" helper.
 *
 * The branch-selector pills (BranchSelector.svelte + GitHeaderActions.svelte)
 * both checkout branches via the git store. Previously their catch blocks only
 * `console.error`'d the failure, so a switch that failed because of uncommitted
 * changes (HTTP 409) silently did nothing — no toast, no offer to stash.
 *
 * This helper runs the checkout and, on failure, surfaces the backend's error
 * via the canonical toast store. When the failure is specifically an
 * uncommitted-changes conflict (409), the error toast carries a
 * "Stash & switch" action that re-runs the checkout with auto-stash enabled.
 */

import { git } from '$lib/stores/git.svelte';
import { isApiError } from '$lib/api/client';
import { notify } from '$lib/stores/notifications';

/**
 * Run a user-initiated branch checkout and surface any failure to the user.
 *
 * @param ref          Branch/ref to check out.
 * @param afterSwitch  Optional callback run after a successful switch (e.g.
 *                     refreshing diff stats). Awaited.
 */
export async function checkoutBranchWithToast(
	ref: string,
	afterSwitch?: () => void | Promise<void>
): Promise<void> {
	await runCheckout(ref, false, afterSwitch);
}

async function runCheckout(
	ref: string,
	autoStash: boolean,
	afterSwitch?: () => void | Promise<void>
): Promise<void> {
	try {
		await git.checkout(ref, autoStash);
		await afterSwitch?.();
		if (autoStash) {
			notify(`Stashed changes and switched to ${ref}`, { severity: 'success' });
		}
	} catch (e) {
		console.error('Failed to checkout branch:', e);

		// Uncommitted-changes conflict (409): offer to stash and retry.
		const isUncommittedConflict =
			!autoStash && isApiError(e) && e.status === 409;

		const detail = isApiError(e)
			? e.detail
			: `Failed to switch to ${ref}`;

		if (isUncommittedConflict) {
			notify(detail, {
				severity: 'error',
				action: {
					label: 'Stash & switch',
					onClick: () => {
						void runCheckout(ref, true, afterSwitch);
					},
				},
			});
		} else {
			notify(detail, { severity: 'error' });
		}
	}
}
