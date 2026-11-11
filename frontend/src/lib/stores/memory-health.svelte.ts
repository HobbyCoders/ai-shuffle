/**
 * The connected worker's memory health (GET /memory/health) for Settings >
 * System > Memory health (#1042), kept outside the page so reopening it shows
 * the last answer at once while it asks again.
 *
 * It is asked only as Settings > System opens: the owner cannot act on a
 * warning (the worker repairs by itself), so nothing in the app's chrome
 * follows it. The worker announces a loss only as one bell notification per
 * stretch (`_announce_health`). AppPage forgets the answer when the owner
 * signs out or loses owner rights.
 */
import { loadState, type LoadState } from '$lib/settings/access.svelte';
import { fetchMemoryHealth, type MemoryHealth } from '$lib/components/settings/pages/memory-health';

let current = $state<LoadState<MemoryHealth> | null>(null);
// A reset retires every request already on its way.
let epoch = 0;
let inflight: Promise<LoadState<MemoryHealth>> | null = null;

function refresh(): Promise<LoadState<MemoryHealth>> {
	if (inflight) return inflight;
	const mine = epoch;
	const request = loadState(fetchMemoryHealth).then((next) => {
		if (mine === epoch) current = next;
		if (inflight === request) inflight = null;
		return next;
	});
	inflight = request;
	return request;
}

export const memoryHealth = {
	/** Null until the first answer (or after a reset). */
	get result(): LoadState<MemoryHealth> | null {
		return current;
	},

	/** Ask the worker now; a request already on its way is shared. */
	refresh,

	/** Forget the answer and drop requests on their way. */
	reset(): void {
		epoch++;
		inflight = null;
		current = null;
	}
};
