/**
 * The cross-worker projects overview (GET /projects/overview): every logical
 * project known to any worker, with each approved worker's state.
 *
 * `projectsOverviewSupported` is null until asked, then true, or false when
 * this worker predates the endpoint; the Projects page then shows the plain
 * list with no worker chips (fail open), and the new-chat pickers keep their
 * classic rules.
 *
 * Kept apart from app-data so the many tests that mock app-data need not
 * know about it.
 */

import { get, writable, type Readable } from 'svelte/store';
import { getProjectsOverview, type ProjectOverviewRow, type ProjectWorker } from '$lib/api/project-workers';

const rows = writable<ProjectOverviewRow[]>([]);
const supported = writable<boolean | null>(null);

export const projectsOverview: Readable<ProjectOverviewRow[]> = { subscribe: rows.subscribe };
export const projectsOverviewSupported: Readable<boolean | null> = { subscribe: supported.subscribe };

let inFlight: Promise<void> | null = null;
let again = false;

async function fetchOnce(): Promise<void> {
	try {
		const next = await getProjectsOverview();
		if (next === null) {
			rows.set([]);
			supported.set(false);
			return;
		}
		rows.set(next);
		supported.set(true);
	} catch (e) {
		// Keep the last good list; a first failure leaves the plain list showing.
		console.error('Failed to load projects overview:', e);
	}
}

/**
 * Load the overview. Concurrent callers share one request; a call while one
 * runs queues exactly one more, so a change seen mid-flight is never lost.
 */
export function loadProjectsOverview(): Promise<void> {
	if (inFlight) {
		again = true;
		return inFlight;
	}
	inFlight = (async () => {
		try {
			do {
				again = false;
				await fetchOnce();
			} while (again);
		} finally {
			inFlight = null;
		}
	})();
	return inFlight;
}

/** Refresh only once something on screen has used it (and the server has it). */
export function refreshProjectsOverviewIfUsed(): Promise<void> {
	return get(supported) === true ? loadProjectsOverview() : Promise.resolve();
}

/**
 * Patch one worker's row at once (a setup just started); the next load
 * replaces it with the server's answer.
 */
export function patchOverviewWorker(projectV2Id: string, installId: string, patch: Partial<ProjectWorker>) {
	rows.update((list) =>
		list.some((r) => r.project_v2_id === projectV2Id)
			? list.map((r) =>
					r.project_v2_id !== projectV2Id
						? r
						: { ...r, workers: r.workers.map((w) => (w.install_id === installId ? { ...w, ...patch } : w)) }
				)
			: list
	);
}

/** Tests: forget everything. */
export function resetProjectsOverview() {
	rows.set([]);
	supported.set(null);
	inFlight = null;
	again = false;
}
