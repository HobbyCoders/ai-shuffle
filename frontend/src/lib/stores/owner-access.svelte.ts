/**
 * Owner password state for the root layout: whether this browser should see
 * the sign-in form, the "create the owner password" step, or the app.
 */
import { getOwnerStatus, type OwnerStatus } from '$lib/api/owner';

let _status = $state<OwnerStatus | null>(null);
let epoch = 0;

export const ownerAccess = {
	/** Null until loaded, or when the worker does not report it. */
	get status(): OwnerStatus | null {
		return _status;
	},

	async refresh(): Promise<OwnerStatus | null> {
		const mine = ++epoch;
		const next = await getOwnerStatus();
		if (mine === epoch) _status = next;
		return _status;
	}
};
