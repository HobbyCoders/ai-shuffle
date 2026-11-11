/**
 * The connected worker's memory health (GET /memory/health), one answer
 * shared by the sidebar's "Needs you" item and Settings > System > Memory
 * health (#1042), so the two never disagree.
 *
 * AppPage starts it for a signed-in owner: it asks once, then every
 * MEMORY_HEALTH_REFRESH_MS, and again whenever Settings > System opens. The
 * worker announces a loss only as one bell notification per stretch
 * (`_announce_health`), which is no state to follow, hence this store.
 *
 * The warning follows the current check: it shows while the check reports
 * attention and goes when it reports healthy. An error, a refusal or an
 * unexpected body is never a warning. Stopping the last holder (sign-out,
 * another account) forgets the answer.
 */
import { loadState, type LoadState } from '$lib/settings/access.svelte';
import { fetchMemoryHealth, memoryAlert, type MemoryHealth } from '$lib/components/settings/pages/memory-health';

/**
 * The worker checks a store at most hourly, sooner after a curation pass; its
 * maintenance sweep wakes every 5 minutes (app/core/fluid_memory.py). A
 * quarter hour sees a new answer soon enough without asking for nothing.
 */
export const MEMORY_HEALTH_REFRESH_MS = 15 * 60_000;

let current = $state<LoadState<MemoryHealth> | null>(null);
// A reset retires every request already on its way.
let epoch = 0;
let inflight: Promise<LoadState<MemoryHealth>> | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let holders = 0;

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

function reset(): void {
	epoch++;
	inflight = null;
	current = null;
}

export const memoryHealth = {
	/** Null until the first answer (or after a reset). */
	get result(): LoadState<MemoryHealth> | null {
		return current;
	},

	/**
	 * The one-line warning while the check reports attention ("Capacity:
	 * Automatic cleanup has recorded…"); empty otherwise.
	 */
	get alert(): string {
		return current?.status === 'ok' && current.data?.state === 'attention' ? memoryAlert(current.data) : '';
	},

	/** Ask the worker now; a request already on its way is shared. */
	refresh,

	/** Ask now and on the interval until every holder has stopped. */
	start(): () => void {
		holders++;
		if (holders === 1) {
			void refresh();
			timer = setInterval(() => void refresh(), MEMORY_HEALTH_REFRESH_MS);
		}
		let stopped = false;
		return () => {
			if (stopped) return;
			stopped = true;
			holders = Math.max(0, holders - 1);
			if (holders > 0) return;
			if (timer) clearInterval(timer);
			timer = null;
			reset();
		};
	},

	/** Forget the answer and drop requests on their way (tests). */
	reset(): void {
		if (timer) clearInterval(timer);
		timer = null;
		holders = 0;
		reset();
	}
};
