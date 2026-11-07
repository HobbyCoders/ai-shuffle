/**
 * How a project's per-worker state reads in the UI: the chips on the
 * Projects list and hero, the Workers tab cards, and the new-chat
 * "Runs on" row notes. Pure functions over the /projects/overview and
 * /projects/workers rows (api/project-workers.ts), so every surface words a
 * state the same way.
 */

import type {
	ProjectKind,
	ProjectOverviewRow,
	ProjectWorker,
	WorkerProjectState
} from '$lib/api/project-workers';

export type ChipTone = 'ready' | 'missing' | 'busy' | 'offline' | 'failed' | 'unknown';

export interface WorkerChip {
	tone: ChipTone;
	/** Visible text, e.g. "Studio · cloning". */
	text: string;
	/** Tooltip and accessible description: the state in a sentence. */
	title: string;
	/** A click sets the project up there. */
	actionable: boolean;
}

/** The setup verb for a project kind. */
export function setupVerb(kind: ProjectKind | null | undefined): string {
	return kind === 'folder' ? 'setting up' : 'cloning';
}

/** A setup the worker is running or about to run (not one waiting for it to come online). */
export function isRunning(worker: Pick<ProjectWorker, 'state' | 'setup'>): boolean {
	if (worker.state === 'setting_up') return true;
	const status = worker.setup?.status;
	return worker.state !== 'queued_offline' && (status === 'queued' || status === 'running');
}

/** Any outstanding setup, including one queued until the worker is back. */
export function isPending(worker: Pick<ProjectWorker, 'state' | 'setup'>): boolean {
	return isRunning(worker) || worker.state === 'queued_offline';
}

/** States a setup can fix (offline_missing queues it until the worker returns). */
export function needsSetup(state: WorkerProjectState | string): boolean {
	return state === 'missing' || state === 'failed' || state === 'offline_missing';
}

/** The worker holds the project (online or not). */
export function hasProject(state: WorkerProjectState | string): boolean {
	return state === 'ready' || state === 'offline_has';
}

export function workerChip(worker: ProjectWorker, kind: ProjectKind | null | undefined): WorkerChip {
	const name = worker.device_name;
	if (isRunning(worker)) {
		const p = worker.setup?.progress;
		const pct = typeof p === 'number' ? ` ${Math.round(p)}%` : '';
		return { tone: 'busy', text: `${name} · ${setupVerb(kind)}${pct}`, title: `Setting up on ${name}…`, actionable: false };
	}
	switch (worker.state) {
		case 'ready':
			return { tone: 'ready', text: name, title: `${name} has this project`, actionable: false };
		case 'missing':
			return { tone: 'missing', text: name, title: `Not on ${name} — click to set it up`, actionable: true };
		case 'failed':
			return {
				tone: 'failed',
				text: `${name} · failed`,
				title: worker.setup?.error || `Setting it up on ${name} failed`,
				actionable: false
			};
		case 'queued_offline':
			return { tone: 'offline', text: `${name} · queued`, title: `Sets up when ${name} is back online`, actionable: false };
		case 'offline_has':
			return { tone: 'offline', text: `${name} · offline · has it`, title: `${name} is offline; it has this project`, actionable: false };
		case 'offline_missing':
			return { tone: 'offline', text: `${name} · offline`, title: `${name} is offline and doesn't have this project`, actionable: false };
		default:
			return { tone: 'unknown', text: name, title: `Couldn't check ${name} right now`, actionable: false };
	}
}

/** Workers that lack the project and are online now (a setup would start at once). */
export function missingOnline(workers: ProjectWorker[]): ProjectWorker[] {
	return workers.filter((w) => w.online && (w.state === 'missing' || w.state === 'failed') && !isPending(w));
}

/** Workers the "N missing" badge counts: lacking it and not already being set up. */
export function missingCount(workers: ProjectWorker[]): number {
	return workers.filter((w) => needsSetup(w.state) && !isPending(w)).length;
}

/** The self worker's row, if the project has one. */
export function selfWorker(workers: ProjectWorker[]): ProjectWorker | undefined {
	return workers.find((w) => w.is_self);
}

/** The project is on this worker. */
export function isOnThisWorker(row: Pick<ProjectOverviewRow, 'local_project_id' | 'workers'>): boolean {
	if (row.local_project_id) return true;
	const self = selfWorker(row.workers);
	return !!self && hasProject(self.state);
}

/** Last path segment, for either separator. */
export function folderLeaf(path: string | null | undefined): string {
	return (path ?? '').split(/[/\\]/).filter(Boolean).pop() ?? '';
}

/**
 * The trailing note of a worker row in the new-chat "Runs on" menu.
 * `null` = no note (state unknown, or no project picked).
 */
export function runsOnNote(worker: ProjectWorker | undefined): string | null {
	if (!worker) return null;
	if (!worker.online || worker.state === 'offline_has' || worker.state === 'offline_missing' || worker.state === 'queued_offline') return 'offline';
	if (isRunning(worker)) return 'setting up';
	if (worker.state === 'ready') return 'ready';
	if (worker.state === 'missing' || worker.state === 'failed') return 'not set up · set up';
	return null;
}

export type ErrorAction = { kind: 'github'; label: string } | { kind: 'folder'; label: string } | null;

/** The fix a failed setup offers besides Retry. */
export function errorAction(code: string | null | undefined, workerName: string): ErrorAction {
	if (code === 'github_auth') return { kind: 'github', label: `Sign in to GitHub on ${workerName}` };
	if (code === 'folder_exists') return { kind: 'folder', label: 'Choose another folder' };
	return null;
}

/** The plain reason for a failed setup, falling back on the code. */
export function failureReason(worker: Pick<ProjectWorker, 'device_name' | 'setup'>): string {
	const s = worker.setup;
	if (s?.error) return s.error;
	switch (s?.error_code) {
		case 'github_auth': return `${worker.device_name} isn't signed in to GitHub, and this repo is private.`;
		case 'network': return `${worker.device_name} couldn't reach the repository.`;
		case 'folder_exists': return 'That folder already exists and holds something else.';
		case 'not_found': return 'The repository no longer exists.';
		case 'offline': return `${worker.device_name} went offline.`;
		case 'unsupported': return `Update ${worker.device_name} to set projects up from here.`;
		default: return 'Something went wrong while setting it up.';
	}
}

/** "seen 3d ago" from an ISO time; null when unknown. */
export function seenAgo(iso: string | null | undefined, now = Date.now()): string | null {
	if (!iso) return null;
	const t = Date.parse(iso);
	if (Number.isNaN(t)) return null;
	const s = Math.max(0, Math.round((now - t) / 1000));
	if (s < 60) return 'seen just now';
	const m = Math.round(s / 60);
	if (m < 60) return `seen ${m}m ago`;
	const h = Math.round(m / 60);
	if (h < 48) return `seen ${h}h ago`;
	return `seen ${Math.round(h / 24)}d ago`;
}

/**
 * The Projects list's "All workers / On <this worker>" filter row (#1210).
 * One rule: whether the row takes space is decided once per visit, from what
 * is known when the list first paints, and never changes under the painted
 * list. Later answers only fill the space with the control or leave it blank.
 *
 * `filterGeometry` makes that decision, or returns null while it cannot yet:
 * - the overview has answered: a row when it reports more than one worker,
 *   none when it reports one or the worker has no overview (an older one);
 * - else the install registry (a plain database read) has answered: a row
 *   when it holds more than one approved worker (the same approved installs
 *   the overview reports), else none;
 * - else, once the registry has failed or `FILTER_DECISION_MS` has passed
 *   with neither answer, a row: more than one worker might be present, and a
 *   blank row is the only choice a late answer can never move.
 *
 * Until it decides the list paints nothing (`unknown`), so a stalled request
 * holds local projects back for that long at most.
 *
 * The row is one line whatever this worker's name is (the name is shortened,
 * with the full name in its tooltip), so filling the held space with a longer
 * name than the one it was held for cannot change its height either.
 */
export const FILTER_DECISION_MS = 2000;

/** A journey's own clock for the deadline: it is handed `pass` and calls it when it chooses. */
type FilterDeadlineHook = (pass: () => void) => void;

/**
 * Start this visit's `FILTER_DECISION_MS` wait: `pass` runs when it is over.
 * Returns the cancel. A UI journey that must measure before the deadline on a
 * slow runner sets `window.__projectsFilterDeadline` before the app loads and
 * passes the deadline itself; nothing else sets it.
 */
export function startFilterDeadline(pass: () => void): () => void {
	const hook = typeof window === 'undefined' ? undefined : (window as Window & { __projectsFilterDeadline?: FilterDeadlineHook }).__projectsFilterDeadline;
	if (typeof hook === 'function') {
		let live = true;
		hook(() => { if (live) pass(); });
		return () => { live = false; };
	}
	const timer = setTimeout(pass, FILTER_DECISION_MS);
	return () => clearTimeout(timer);
}

export type FilterGeometry = 'row' | 'none';

export function filterGeometry(known: {
	/** `projectsOverviewSupported`: null until the overview has answered. */
	supported: boolean | null;
	overviewWorkerCount: number;
	/** The install registry, null until loaded. */
	installs: ReadonlyArray<{ approved?: boolean }> | null;
	/** This visit's registry request failed. */
	registryFailed: boolean;
	/** This visit has waited `FILTER_DECISION_MS` for an answer. */
	deadlinePassed: boolean;
}): FilterGeometry | null {
	if (known.supported === true) return known.overviewWorkerCount > 1 ? 'row' : 'none';
	if (known.supported === false) return 'none';
	// An older server leaves `approved` out: that means approved.
	if (known.installs) return known.installs.filter((i) => i.approved !== false).length > 1 ? 'row' : 'none';
	if (known.registryFailed || known.deadlinePassed) return 'row';
	return null;
}

/**
 * What the filter region shows, given the visit's decision: `unknown` (not
 * decided: the list waits), `hidden` (no row, no space), `shown` (the control:
 * the overview reports more than one worker) or `reserved` (the row's space,
 * its control invisible and inert, because the overview has not answered yet,
 * failed, or answered "no filter" after the space was taken).
 */
export type WorkerFilterSlot = 'unknown' | 'hidden' | 'reserved' | 'shown';

export function workerFilterSlot(geometry: FilterGeometry | null, supported: boolean | null, overviewWorkerCount: number): WorkerFilterSlot {
	if (geometry === null) return 'unknown';
	if (geometry === 'none') return 'hidden';
	return supported === true && overviewWorkerCount > 1 ? 'shown' : 'reserved';
}
