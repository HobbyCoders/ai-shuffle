/**
 * System > Updates state: the update policy (two switches) and "Check for
 * updates".
 *
 * The policy is polled every 5 s while the page is mounted, as before; a read
 * never lands under a save. A policy change tells the sidebar update button to
 * re-read (`application-update-policy` event).
 *
 * A check posts `{action: 'check'}`, then polls the workers every 2 s for up
 * to 90 s. A worker counts as settled once it has left "checking" after being
 * seen in it, reports an outcome (available, ready, error...), or has stayed
 * idle for 10 s (a check that finished before the first poll). Workers that
 * refused the request show the reason they gave.
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
}

/**
 * What a worker's update state means after a check. Phases come from
 * app/core/application_updates.py: idle, checking, downloading,
 * available, ready, error, image-required, unsupported.
 */
export function describeWorker(worker: UpdateWorker, receipt?: UpdateReceipt): { tone: StatusTone; text: string } {
	if (receipt && receipt.accepted === false) return { tone: 'off', text: receipt.reason || 'Not checked' };
	if (!worker.online) return { tone: 'off', text: 'Offline' };
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
		case 'image-required':
			return { tone: 'err', text: 'Needs a new runtime release' };
		case 'unsupported':
			return { tone: 'off', text: worker.reason || 'Status unavailable' };
		default:
			return { tone: 'ok', text: 'Up to date' };
	}
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
	/** Bumps on every failed save, so the page can redraw controls that showed the unsaved value. */
	failures = $state(0);

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

	async set(key: keyof UpdatePolicy, value: boolean): Promise<void> {
		if (!this.policy || this.saving) return;
		this.saving = true;
		this.error = '';
		try {
			const next = await api.put<UpdatePolicy>('/settings/application-updates', { [key]: value });
			if (!this.#alive) return;
			this.policy = { allow_updates: next.allow_updates, update_all_workers: next.update_all_workers };
			window.dispatchEvent(new Event(POLICY_EVENT));
			flashSaved();
		} catch (cause) {
			if (!this.#alive) return;
			this.error = getErrorMessage(cause, 'Could not save update setting');
			this.failures += 1;
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
				for (const worker of snapshot.workers) {
					const receipt = receipts.find((r) =>
						r.device_id ? r.device_id === worker.device_id : r.device_name === worker.device_name
					);
					if (worker.phase === 'checking') seenChecking.add(worker.device_id);
					const refused = receipt?.accepted === false || !worker.online;
					const done =
						refused ||
						(worker.phase !== 'checking' &&
							(seenChecking.has(worker.device_id) ||
								OUTCOME_PHASES.has(worker.phase) ||
								elapsed >= CHECK_IDLE_SETTLE_MS));
					if (!done) settled = false;
					const view = done ? describeWorker(worker, receipt) : { tone: 'busy' as const, text: 'Checking' };
					rows.push({ id: worker.device_id, name: worker.device_name, ...view });
				}
				this.rows = rows;
			} else settled = false;
			if (settled || elapsed >= CHECK_TIMEOUT_MS) {
				if (!settled && this.rows) {
					this.rows = this.rows.map((row) =>
						row.tone === 'busy' && row.text === 'Checking' ? { ...row, tone: 'off', text: 'No answer yet' } : row
					);
				}
				this.checking = false;
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
