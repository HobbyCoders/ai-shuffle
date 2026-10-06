/**
 * System > Updates state: the update policy (two switches) and "Check for
 * updates".
 *
 * The policy is polled every 5 s while the page is mounted, as before; a read
 * never lands under a save. A policy change tells the sidebar update button to
 * re-read (`application-update-policy` event).
 *
 * A check posts `{action: 'check'}`, then polls the workers every 2 s for up
 * to 90 s. A worker that still has the request queued (`request_pending`)
 * or is "checking" is not settled; otherwise it counts as settled once it has
 * left "checking" after being seen in it, reports an outcome (available,
 * ready, error...), or has stayed idle for 10 s (a check that finished before
 * the first poll). Workers that refused the request show the reason they
 * gave. The sidebar update button re-reads when the check is sent and when
 * it ends.
 */
import { api, getErrorMessage } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import type { StatusTone } from '$lib/settings/types';

/** One worker in GET /settings/application-updates/workers. */
export interface UpdateWorker {
	device_id: string;
	device_name: string;
	is_self?: boolean;
	online: boolean;
	supported: boolean;
	allow_updates?: boolean;
	phase: string;
	version?: string | null;
	running_version?: string | null;
	percent?: number | null;
	message?: string | null;
	error?: string | null;
	reason?: string | null;
	/** The check request is queued and the worker has not taken it yet. */
	request_pending?: boolean;
	/** A coming update needs a newer Docker image (docker/application_updates.py). */
	notice?: string | null;
}

/** One worker's answer to POST /settings/application-updates {action: 'check'}. */
export interface UpdateReceipt {
	device_id?: string;
	device_name: string;
	accepted: boolean | null;
	reason: string;
}

export interface UpdateResultRow {
	id: string;
	name: string;
	tone: StatusTone;
	text: string;
	/** What to do about it, shown once under the list. */
	hint?: string;
}

const IMAGE_HOW =
	'To get a newer Docker image: in Unraid, choose Force update on the AI Shuffle container; with Docker Compose, run docker compose pull, then docker compose up -d. Your data stays on the data volume.';

/** How to replace a Docker image, for a worker whose update needs one now (#1821). */
export const IMAGE_HINT = `Your current version keeps working. ${IMAGE_HOW}`;

/** The image hint for a worker that needs a newer image now, or for a coming update. */
export function imageHint(worker: UpdateWorker): string | undefined {
	if (worker.phase === 'image-required') return IMAGE_HINT;
	return worker.notice ? `${worker.notice} ${IMAGE_HOW}` : undefined;
}

/**
 * What a worker's update state means after a check. Phases come from
 * app/core/application_updates.py: idle, checking, downloading,
 * available, ready, error, image-required, unsupported.
 */
export function describeWorker(worker: UpdateWorker, receipt?: UpdateReceipt): { tone: StatusTone; text: string } {
	if (receipt && receipt.accepted === false) return { tone: 'off', text: receipt.reason || 'Not checked' };
	if (!worker.online) return { tone: 'off', text: 'Offline' };
	if (worker.request_pending) return { tone: 'busy', text: 'Checking' };
	// The container updater reports the running revision with this phase (#1901).
	if (worker.phase === 'image-required') return { tone: 'err', text: worker.message || 'Needs a new Docker image' };
	if (worker.phase !== 'checking' && sameVersion(worker.version, worker.running_version)) {
		return { tone: 'ok', text: 'Up to date' };
	}
	switch (worker.phase) {
		case 'checking':
			return { tone: 'busy', text: 'Checking' };
		case 'downloading':
			return { tone: 'busy', text: worker.percent != null ? `Downloading, ${Math.round(worker.percent)}%` : 'Downloading' };
		case 'available':
			return { tone: 'warn', text: worker.version ? `Version ${worker.version} available` : 'Update available' };
		case 'ready':
			return { tone: 'warn', text: 'Update downloaded, ready to install' };
		case 'error':
			return { tone: 'err', text: worker.error || 'Check failed' };
		case 'unsupported':
			return { tone: 'off', text: worker.reason || 'Status unavailable' };
		default:
			return { tone: 'ok', text: 'Up to date' };
	}
}

/** A build's version and the running one match (either may carry a suffix). */
function sameVersion(version?: string | null, running?: string | null): boolean {
	return !!version && !!running && (running.startsWith(version) || version.startsWith(running));
}

export interface UpdatePolicy {
	allow_updates: boolean;
	update_all_workers: boolean;
}

interface Snapshot extends UpdatePolicy {
	workers: UpdateWorker[];
}

export const POLICY_POLL_MS = 5_000;
export const CHECK_POLL_MS = 2_000;
export const CHECK_TIMEOUT_MS = 90_000;
/** An idle worker this long after the request has finished (or never started) its check. */
export const CHECK_IDLE_SETTLE_MS = 10_000;
export const POLICY_EVENT = 'application-update-policy';

const OUTCOME_PHASES = new Set(['available', 'ready', 'error', 'image-required', 'unsupported', 'downloading']);

export class UpdatesState {
	policy = $state<UpdatePolicy | null>(null);
	status = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	saving = $state(false);
	error = $state('');

	/** Check results, null until a check runs. */
	rows = $state<UpdateResultRow[] | null>(null);
	checking = $state(false);
	checkError = $state('');

	#alive = true;
	#policyTimer: ReturnType<typeof setInterval> | null = null;
	#checkTimer: ReturnType<typeof setTimeout> | null = null;
	#run = 0;

	start(): void {
		void this.refresh();
		this.#policyTimer = setInterval(() => void this.refresh(), POLICY_POLL_MS);
	}

	async refresh(): Promise<void> {
		try {
			const next = await api.get<UpdatePolicy>('/settings/application-updates');
			if (!this.#alive || this.saving) return;
			this.policy = { allow_updates: next.allow_updates, update_all_workers: next.update_all_workers };
			this.status = 'ok';
		} catch (cause) {
			if (!this.#alive) return;
			if (isForbidden(cause)) this.status = 'forbidden';
			else if (!this.policy) {
				this.status = 'error';
				this.error = getErrorMessage(cause, 'Could not load update settings');
			}
		}
	}

	async set(key: keyof UpdatePolicy, value: boolean): Promise<boolean> {
		if (!this.policy || this.saving) return false;
		this.saving = true;
		this.error = '';
		try {
			const next = await api.put<UpdatePolicy>('/settings/application-updates', { [key]: value });
			if (!this.#alive) return false;
			this.policy = { allow_updates: next.allow_updates, update_all_workers: next.update_all_workers };
			window.dispatchEvent(new Event(POLICY_EVENT));
			flashSaved();
			return true;
		} catch (cause) {
			if (!this.#alive) return false;
			this.error = getErrorMessage(cause, 'Could not save update setting');
			return false;
		} finally {
			this.saving = false;
		}
	}

	async check(): Promise<void> {
		if (this.checking || !this.policy?.allow_updates) return;
		const run = ++this.#run;
		this.checking = true;
		this.checkError = '';
		this.rows = [];
		const started = Date.now();
		let receipts: UpdateReceipt[] = [];
		try {
			const result = await api.post<{ results: UpdateReceipt[] }>('/settings/application-updates', { action: 'check' });
			receipts = result?.results ?? [];
			window.dispatchEvent(new Event(POLICY_EVENT));
		} catch (cause) {
			if (!this.#alive || run !== this.#run) return;
			this.checkError = getErrorMessage(cause, 'Could not check for updates');
			this.checking = false;
			this.rows = null;
			return;
		}
		const seenChecking = new Set<string>();
		const poll = async () => {
			if (!this.#alive || run !== this.#run) return;
			let snapshot: Snapshot | null = null;
			try {
				snapshot = await api.get<Snapshot>('/settings/application-updates/workers');
			} catch {
				// A worker restarting mid-check: keep polling until the deadline.
			}
			if (!this.#alive || run !== this.#run) return;
			const elapsed = Date.now() - started;
			let settled = true;
			if (snapshot) {
				const rows: UpdateResultRow[] = [];
				// One row per worker that was asked (receipts include refusals for
				// workers the snapshot no longer lists); without receipts, every worker.
				const asked: { worker?: UpdateWorker; receipt?: UpdateReceipt }[] = receipts.length
					? receipts.map((receipt) => ({
							receipt,
							worker: snapshot!.workers.find((w) =>
								receipt.device_id ? w.device_id === receipt.device_id : w.device_name === receipt.device_name
							)
						}))
					: snapshot.workers.map((worker) => ({ worker }));
				for (const [index, { worker, receipt }] of asked.entries()) {
					const name = worker?.device_name ?? receipt?.device_name ?? 'Worker';
					const id = worker?.device_id ?? receipt?.device_id ?? `receipt-${index}`;
					if (!worker) {
						rows.push({
							id,
							name,
							...(receipt?.accepted === false
								? { tone: 'off' as const, text: receipt.reason || 'Not checked' }
								: { tone: 'off' as const, text: 'Status unavailable' })
						});
						continue;
					}
					// Queued counts as started: once it is neither queued nor checking, the check ran.
					if (worker.phase === 'checking' || worker.request_pending) seenChecking.add(worker.device_id);
					const refused = receipt?.accepted === false || !worker.online;
					const done =
						refused ||
						(worker.phase !== 'checking' &&
							!worker.request_pending &&
							(seenChecking.has(worker.device_id) ||
								OUTCOME_PHASES.has(worker.phase) ||
								elapsed >= CHECK_IDLE_SETTLE_MS));
					if (!done) settled = false;
					const view = done ? describeWorker(worker, receipt) : { tone: 'busy' as const, text: 'Checking' };
					rows.push({ id, name, ...view, ...(done && imageHint(worker) ? { hint: imageHint(worker) } : {}) });
				}
				this.rows = rows;
			} else settled = false;
			if (settled || elapsed >= CHECK_TIMEOUT_MS) {
				if (!settled && this.rows) {
					this.rows = this.rows.map((row) =>
						row.tone === 'busy' && row.text === 'Checking' ? { ...row, tone: 'off', text: 'Still checking' } : row
					);
				}
				this.checking = false;
				window.dispatchEvent(new Event(POLICY_EVENT));
				return;
			}
			this.#checkTimer = setTimeout(() => void poll(), CHECK_POLL_MS);
		};
		await poll();
	}

	destroy(): void {
		this.#alive = false;
		++this.#run;
		if (this.#policyTimer) clearInterval(this.#policyTimer);
		if (this.#checkTimer) clearTimeout(this.#checkTimer);
	}
}
