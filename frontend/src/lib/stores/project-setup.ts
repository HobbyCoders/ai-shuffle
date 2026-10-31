/**
 * Setting a project up on a worker and waiting for it to be ready.
 *
 * `waitForProjectSetup` polls GET /projects/workers every 2 s while the
 * setup is queued or running (the server also emits a `projects`
 * resources_changed on every state change, which pokes the poll early), and
 * settles when that worker's row reads ready or failed.
 *
 * `newChatSetup` is the new-chat flow on a worker without the project
 * ("Set up & send"): the need is derived from the picked project, the picked
 * worker and the cross-worker overview; `run` sets it up there and resolves
 * true once the held message may be sent.
 */

import { derived, get, writable, type Readable } from 'svelte/store';
import {
	getProjectWorkers,
	projectRef,
	startProjectSetup,
	type ProjectKind,
	type ProjectRef,
	type ProjectWorker,
	type ProjectWorkerDetail
} from '$lib/api/project-workers';
import type { Install } from '$lib/api/installs';
import { resourceSync } from '$lib/services/resource-sync';
import { activeSession } from './active-session';
import { appData } from './app-data.svelte';
import { loadProjectsOverview, patchOverviewWorker, projectsOverview, projectsOverviewSupported } from './projects-overview';
import { matches, workerInstalls } from './worker-presence';
import { failureReason, isPending } from '$lib/components/deck/cards/project-workers-view';

/** Poll period while a setup is queued or running (spec: 2 s). */
export const SETUP_POLL_MS = 2_000;

export class ProjectSetupError extends Error {
	constructor(message: string, readonly code: string | null) {
		super(message);
	}
}

/** Whether an overview/detail worker row stands for this install record (any of its ids). */
export function workerMatchesInstall(worker: Pick<ProjectWorker, 'install_id'>, install: Install | null | undefined, id: string): boolean {
	if (worker.install_id === id) return true;
	return !!install && matches(install, worker.install_id);
}

/**
 * Wait until `installId` holds the project. Rejects with a
 * `ProjectSetupError` on a failed or cancelled setup; an abort rejects with
 * the signal's reason.
 */
export function waitForProjectSetup(
	ref: ProjectRef,
	installId: string,
	options: { signal?: AbortSignal; intervalMs?: number; onUpdate?: (worker: ProjectWorkerDetail) => void } = {}
): Promise<ProjectWorkerDetail> {
	const interval = options.intervalMs ?? SETUP_POLL_MS;
	return new Promise((resolve, reject) => {
		let timer: ReturnType<typeof setTimeout> | undefined;
		let done = false;
		let polling = false;
		let again = false;
		let sawPending = false;
		let idleReads = 0;
		const stopWatching = resourceSync.subscribe(['projects'], () => poke(), { whileHidden: true });
		const finish = (fn: () => void) => {
			if (done) return;
			done = true;
			if (timer) clearTimeout(timer);
			stopWatching();
			options.signal?.removeEventListener('abort', onAbort);
			fn();
		};
		const onAbort = () => finish(() => reject(options.signal?.reason ?? new DOMException('Aborted', 'AbortError')));
		options.signal?.addEventListener('abort', onAbort);
		if (options.signal?.aborted) return onAbort();

		function schedule() {
			if (done) return;
			if (timer) clearTimeout(timer);
			timer = setTimeout(() => void poll(), interval);
		}
		function poke() {
			if (done) return;
			if (polling) { again = true; return; }
			void poll();
		}
		async function poll() {
			if (done) return;
			polling = true;
			try {
				const res = await getProjectWorkers(ref, { signal: options.signal });
				if (done) return;
				const worker = res.workers.find((w) => w.install_id === installId);
				if (worker) {
					options.onUpdate?.(worker);
					if (worker.state === 'ready') return finish(() => resolve(worker));
					if (worker.state === 'failed' || worker.setup?.status === 'error') {
						return finish(() => reject(new ProjectSetupError(failureReason(worker), worker.setup?.error_code ?? null)));
					}
					if (worker.setup?.status === 'cancelled') {
						return finish(() => reject(new ProjectSetupError('The setup was cancelled.', 'cancelled')));
					}
					if (isPending(worker)) sawPending = true;
					else if (++idleReads >= 3 && sawPending) {
						return finish(() => reject(new ProjectSetupError('The setup stopped before it finished.', 'cancelled')));
					}
				}
			} catch (e) {
				if (done) return;
				// A transient read failure keeps polling; the hold has its own way out (Cancel).
				console.warn('[project-setup] poll failed:', e);
			} finally {
				polling = false;
			}
			if (again) { again = false; void poll(); } else schedule();
		}
		void poll();
	});
}

// ─── New chat on a worker without the project ────────────────────────────────

export interface NewChatSetupNeed {
	ref: ProjectRef;
	projectV2Id: string;
	installId: string;
	workerName: string;
	projectName: string;
	kind: ProjectKind;
	/** A previous attempt failed (the row says failed). */
	failed: boolean;
}

/**
 * The project a new chat is about to use is not on the worker it will run
 * on, and a setup there can fix that. Null when no new chat, no project, an
 * older server (no overview), the state is unknown, or the target is offline.
 */
export function computeNewChatSetupNeed(
	session: { sessionId: string | null; project: string; projectV2Id: string | null; deviceIntent: { id: string; name: string } | null },
	overview: { project_v2_id: string; local_project_id: string | null; name: string; kind: ProjectKind; workers: ProjectWorker[] }[],
	supported: boolean | null,
	installs: Install[] | null
): NewChatSetupNeed | null {
	if (session.sessionId || supported !== true) return null;
	if (!session.project && !session.projectV2Id) return null;
	const row = overview.find((r) =>
		(session.projectV2Id && r.project_v2_id === session.projectV2Id) ||
		(session.project && r.local_project_id === session.project));
	if (!row) return null;
	const intent = session.deviceIntent;
	const install = intent ? installs?.find((i) => matches(i, intent.id)) : installs?.find((i) => i.is_self);
	const worker = intent
		? row.workers.find((w) => workerMatchesInstall(w, install, intent.id))
		: row.workers.find((w) => w.is_self);
	if (!worker || !worker.online) return null;
	if (worker.state !== 'missing' && worker.state !== 'failed' && worker.state !== 'setting_up') return null;
	return {
		ref: projectRef(row.project_v2_id, null)!,
		projectV2Id: row.project_v2_id,
		installId: worker.install_id,
		workerName: worker.device_name,
		projectName: row.name,
		kind: row.kind,
		failed: worker.state === 'failed'
	};
}

export const newChatSetupNeed: Readable<NewChatSetupNeed | null> = derived(
	[activeSession, projectsOverview, projectsOverviewSupported, workerInstalls],
	([$session, $overview, $supported, $installs]) => computeNewChatSetupNeed($session, $overview, $supported, $installs),
	null
);

export interface NewChatSetupState {
	phase: 'idle' | 'running' | 'failed';
	/** `${projectV2Id}:${installId}` the phase belongs to. */
	key: string | null;
	workerName: string;
	progress: number | null;
	reason: string | null;
	code: string | null;
}

const IDLE: NewChatSetupState = { phase: 'idle', key: null, workerName: '', progress: null, reason: null, code: null };
const state = writable<NewChatSetupState>(IDLE);
let controller: AbortController | null = null;

export const newChatSetup = {
	subscribe: state.subscribe,

	get current(): NewChatSetupState { return get(state); },

	/**
	 * Set the project up on the need's worker and wait. True when it is ready
	 * and the held message may go; false on failure (the reason is in the
	 * state), cancel, or a newer run.
	 */
	async run(need: NewChatSetupNeed): Promise<boolean> {
		controller?.abort();
		const mine = new AbortController();
		controller = mine;
		const key = `${need.projectV2Id}:${need.installId}`;
		state.set({ phase: 'running', key, workerName: need.workerName, progress: null, reason: null, code: null });
		try {
			const started = await startProjectSetup(need.ref, { install_id: need.installId });
			if (mine.signal.aborted) return false;
			patchOverviewWorker(need.projectV2Id, need.installId, {
				state: started.status === 'done' ? 'ready' : 'setting_up',
				setup: { op_id: started.op_id, status: started.status === 'done' ? 'done' : 'queued', progress: null }
			});
			if (started.status !== 'done') {
				await waitForProjectSetup(need.ref, need.installId, {
					signal: mine.signal,
					onUpdate: (w) => {
						if (controller === mine) state.update((s) => ({ ...s, progress: w.setup?.progress ?? null }));
					}
				});
			}
			if (controller !== mine) return false;
			// The target now holds the project: refresh what the send resolves against.
			await Promise.all([appData.loadProjectCatalog(performance.now()), loadProjectsOverview()]);
			if (controller !== mine) return false;
			controller = null;
			state.set(IDLE);
			return true;
		} catch (e) {
			if (controller !== mine) return false;
			controller = null;
			if (mine.signal.aborted) { state.set(IDLE); return false; }
			const err = e as { message?: string; detail?: string; code?: string | null };
			const reason = err instanceof ProjectSetupError ? err.message : (err.detail || err.message || 'Setup failed.');
			state.set({ phase: 'failed', key, workerName: need.workerName, progress: null, reason, code: err instanceof ProjectSetupError ? err.code : null });
			void loadProjectsOverview();
			return false;
		}
	},

	/** Stop waiting (the setup itself keeps going on the worker unless cancelled there). */
	abandon() {
		controller?.abort();
		controller = null;
		state.set(IDLE);
	},

	/** Forget a failure, e.g. once the picked project or worker changed. */
	clearFailure() {
		if (get(state).phase === 'failed') state.set(IDLE);
	}
};
