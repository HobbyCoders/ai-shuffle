/**
 * Restart waiting state for application updates (#344): why a downloaded
 * update has not restarted yet, what still runs, and what the owner can do.
 *
 * The fields come from GET /settings/application-updates/workers and install
 * receipts (app/core/update_workers.py waiting_status(), busy_work()) and the
 * aggregate install `operation` (app/core/update_progress.py _with_outcome()).
 * Older peers omit them. Null or omitted means unknown, never "nothing runs".
 *
 * Shared by the sidebar update button and System > Updates; no imports, so
 * the sidebar does not pull in settings state.
 */

export type InstallState = 'idle' | 'ready' | 'blocked' | 'waiting_for_idle' | 'stopping' | 'queued' | 'unavailable';
export type WaitReason =
	| 'running_work'
	| 'still_busy_after_stop'
	| 'stopping_work'
	| 'updater_acknowledgement'
	| 'newer_offer'
	| 'preparing_update'
	| 'updater_idle'
	| 'updater_error';

/** One thing that keeps a worker from restarting: a running turn or a task in a chat. */
export interface Blocker {
	session_id: string;
	title?: string | null;
	/** service, turn, shell, agent, remote, registry (others may pass through). */
	kind?: string | null;
	since?: string | null;
	live?: boolean | null;
	liveness?: string | null;
	running_turn?: boolean | null;
	turn_ended_at?: string | null;
	turn_state?: string | null;
	task_id?: string | null;
	/** Redacted display label. Never fall back to anything else. */
	label?: string | null;
	port?: number | null;
	started_at?: string | null;
	stop_will_stop?: boolean | null;
	monitor_serving_idle_seconds?: number | null;
	/** Approximate epoch seconds; null means no known estimate. */
	idle_stop_at?: number | null;
}

export interface StopResult {
	request_id?: string | null;
	accepted?: boolean | null;
	reason?: string | null;
	completed_at?: number | null;
}

export interface WaitingFields {
	phase?: string;
	version?: string | null;
	message?: string | null;
	error?: string | null;
	percent?: number | null;
	/** Legacy "Restart when they finish" flag; older peers have only this. */
	deferred?: boolean;
	staged_version?: string | null;
	install_state?: InstallState | string | null;
	wait_reason?: WaitReason | string | null;
	blocked_by?: Blocker[] | null;
	restart_expires_at?: number | null;
	monitor_serving_idle_seconds?: number | null;
	stop_result?: StopResult | null;
}

export interface TimeOptions {
	/** Epoch milliseconds; defaults to now. */
	now?: number;
	locale?: string;
	/** Defaults to the client's timezone. */
	timeZone?: string;
}

/**
 * A server time in the client's timezone: "2:05 PM" today, "Oct 20, 2:05 PM"
 * otherwise. Accepts ISO strings and epoch seconds; null when unknown.
 */
export function formatWhen(value: string | number | null | undefined, options: TimeOptions = {}): string | null {
	const ms = typeof value === 'number' ? value * 1000 : typeof value === 'string' && value ? Date.parse(value) : NaN;
	if (!Number.isFinite(ms)) return null;
	const { timeZone, locale } = options;
	const day = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
	const today = day.format(new Date(options.now ?? Date.now())) === day.format(new Date(ms));
	return new Intl.DateTimeFormat(locale, {
		timeZone,
		hour: 'numeric',
		minute: '2-digit',
		...(today ? {} : { month: 'short', day: 'numeric' })
	}).format(new Date(ms));
}

/** A revision short enough to read; non-hex versions stay as they are. */
export function shortRevision(version?: string | null): string {
	if (!version) return 'update';
	return version.length > 12 && /^[0-9a-f]+$/i.test(version) ? version.slice(0, 12) : version;
}

function sameRevision(a?: string | null, b?: string | null): boolean {
	return !!a && !!b && (a.startsWith(b) || b.startsWith(a));
}

const KIND_WORD: Record<string, string> = {
	shell: 'background command',
	agent: 'background agent',
	remote: 'task on another worker',
	registry: 'running work'
};

function chatName(blocker: Blocker): string {
	return blocker.title?.trim() || 'a chat';
}

/** The service or task by its label only; the turn by its chat. */
export function blockerName(blocker: Blocker): string {
	if (blocker.kind === 'turn') return `${chatName(blocker)} (running turn)`;
	return blocker.label?.trim() || (blocker.kind === 'service' ? 'a service' : KIND_WORD[blocker.kind ?? ''] ?? 'running work');
}

export interface BlockerView {
	key: string;
	kind: string;
	text: string;
	/** Short facts under the line: automatic stop estimate, what Stop does. */
	meta: string[];
}

function idleStop(blocker: Blocker, bound: number | null | undefined, time: TimeOptions): string {
	const limit = blocker.monitor_serving_idle_seconds ?? bound;
	const at = limit === 0 ? null : formatWhen(blocker.idle_stop_at, time);
	return at ? `Stops on its own around ${at}, if the chat stays idle.` : 'No known automatic stop time.';
}

/** One readable line per blocker, following the #344 copy table. */
export function blockerView(blocker: Blocker, index: number, bound?: number | null, time: TimeOptions = {}): BlockerView {
	const kind = blocker.kind || 'registry';
	const key = `${blocker.session_id}:${blocker.task_id ?? kind}:${index}`;
	const chat = chatName(blocker);
	if (kind === 'service') {
		const label = blocker.label?.trim();
		const noun = label && /preview/i.test(label) ? 'Preview server' : 'Service';
		const started = formatWhen(blocker.started_at ?? blocker.since, time);
		const ended = formatWhen(blocker.turn_ended_at, time);
		let turn = '';
		if (blocker.running_turn === false) turn = ended ? `; the chat's turn ended ${ended}` : '; chat has no running turn';
		const text =
			`${noun}${label ? ` ${label}` : ''} ${blocker.live ? 'still running' : 'still registered (process not confirmed)'} for ${chat}` +
			`${blocker.port ? ` on port ${blocker.port}` : ''}${started ? `, started ${started}` : ''}${turn}.`;
		const meta = [idleStop(blocker, bound, time)];
		if (blocker.stop_will_stop) meta.push('Stopping running work stops it.');
		return { key, kind, text, meta };
	}
	if (kind === 'turn') {
		const since = formatWhen(blocker.since, time);
		return { key, kind, text: `Waiting for ${chat}: running turn${since ? `, started ${since}` : ''}.`, meta: [] };
	}
	return { key, kind, text: `Waiting for ${chat}: ${blockerName(blocker)}.`, meta: [] };
}

export type WaitingTone = 'ok' | 'warn' | 'err' | 'off' | 'busy';

export interface WaitingView {
	state: InstallState;
	reason: string | null;
	tone: WaitingTone;
	/** Short label for a worker row. */
	title: string;
	/** The sentence the copy table gives for this state. */
	text: string;
	/** Running work, when the state is about it. Null: details unknown. */
	blockers: BlockerView[] | null;
	/** Expiry, last stop attempt. */
	notes: string[];
	/** Which restart controls fit this state. */
	controls: { stop: boolean; whenIdle: boolean; cancel: boolean };
	/** A stop or a restart request is in flight: no duplicate stop/install. */
	locked: boolean;
}

const NO_CONTROLS = { stop: false, whenIdle: false, cancel: false };

/** All one sentence, for a title attribute, a screen reader or the bell. */
export function waitingSummary(view: WaitingView): string {
	return [view.text, ...(view.blockers ?? []).map((b) => b.text), ...view.notes].join(' ');
}

/**
 * What a worker's restart is waiting for, or null when the worker has no
 * waiting state to show (legacy peer, unknown, or nothing downloaded).
 */
export function waitingView(worker: WaitingFields, time: TimeOptions = {}): WaitingView | null {
	const state = (worker.install_state ?? (worker.deferred ? 'waiting_for_idle' : null)) as InstallState | null;
	const reason = worker.wait_reason ?? null;
	if (!state || state === 'idle') return null;
	const bound = worker.monitor_serving_idle_seconds;
	// Null blocked_by is unknown: never render it as "nothing is running".
	const blockers = Array.isArray(worker.blocked_by) ? worker.blocked_by.map((b, i) => blockerView(b, i, bound, time)) : null;
	const base = { state, reason, blockers: null, notes: [] as string[], controls: NO_CONTROLS, locked: false };
	switch (state) {
		case 'ready':
			return { ...base, tone: 'warn', title: 'Downloaded and ready to restart', text: 'Downloaded and ready to restart.' };
		case 'blocked': {
			const controls = { stop: true, whenIdle: true, cancel: false };
			if (reason === 'still_busy_after_stop') {
				const names = (worker.blocked_by ?? []).map(blockerName);
				const named = names.length ? names.join(', ') : 'running work';
				const why = worker.stop_result?.accepted === false && worker.stop_result.reason ? worker.stop_result.reason : 'work is still running';
				const when = formatWhen(worker.stop_result?.completed_at, time);
				return {
					...base,
					tone: 'err',
					title: 'Still running after the stop attempt',
					text: `The stop attempt finished, but ${named} ${names.length > 1 ? 'are' : 'is'} still running. Restart was not queued: ${why.replace(/\.$/, '')}.`,
					blockers,
					notes: when ? [`Last stop attempt ${when}.`] : [],
					controls
				};
			}
			return {
				...base,
				tone: 'warn',
				title: 'Ready; waiting for running work',
				text: blockers ? 'Downloaded and ready.' : 'Downloaded and ready. Running work details are unavailable.',
				blockers,
				controls
			};
		}
		case 'stopping':
			return {
				...base,
				tone: 'busy',
				title: 'Stopping running work…',
				text: 'Stop requested. Stopping running work, then requesting restart…',
				locked: true
			};
		case 'waiting_for_idle': {
			const expires = formatWhen(worker.restart_expires_at, time);
			return {
				...base,
				tone: 'warn',
				title: 'Restarts when work finishes',
				text: 'Will restart when running work finishes.',
				blockers,
				notes: expires ? [`Waits until ${expires} at most; a worker restart drops the schedule.`] : [],
				controls: { stop: true, whenIdle: false, cancel: true }
			};
		}
		case 'queued':
			return { ...base, tone: 'busy', title: 'Restart requested', text: 'Restart requested; waiting for the updater…', locked: true };
		case 'unavailable': {
			if (reason === 'newer_offer') {
				return {
					...base,
					tone: 'warn',
					title: 'Previous download retained',
					text: `Downloaded ${shortRevision(worker.staged_version)} is retained. A newer update is available; download it to continue.`
				};
			}
			if (reason === 'preparing_update') {
				const pct = typeof worker.percent === 'number' ? ` ${Math.round(worker.percent)}%` : '';
				return {
					...base,
					tone: 'busy',
					title: 'Previous download retained',
					text: `Previous download retained. ${worker.message?.trim() || 'Preparing update…'}${pct}`
				};
			}
			if (reason === 'updater_error') {
				return {
					...base,
					tone: 'err',
					title: 'Previous download retained',
					text: `Previous download retained. ${worker.error?.trim() || worker.message?.trim() || 'The updater reported an error.'}`
				};
			}
			return {
				...base,
				tone: 'off',
				title: 'Previous download retained',
				text: 'Previous download retained; the updater is idle. Check for updates before restarting.'
			};
		}
		default:
			return null;
	}
}

/**
 * Release name and notes describe the offered `version`. A staged revision
 * that differs from it must never wear them.
 */
export function offerDescribesStaged(worker: WaitingFields): boolean {
	return !worker.staged_version || !worker.version || sameRevision(worker.staged_version, worker.version);
}

/* ---------- action receipts ---------- */

export interface ActionResult {
	device_id?: string;
	device_name: string;
	accepted: boolean | null;
	reason: string;
	/** "not_sent": nothing was delivered to this worker (a preflight refusal). */
	request_state?: string | null;
	mode?: string | null;
	install_state?: string | null;
}

export function notSent(result: ActionResult): boolean {
	return result.request_state === 'not_sent';
}

/** An install receipt whose waiting fields are present but null: the details failed to load. */
export function waitingUnknown(result: ActionResult): boolean {
	return 'install_state' in result && result.install_state === null;
}

/** The receipt rows worth showing: refusals, and accepted rows whose waiting details are unknown. */
export function receiptRows<T extends ActionResult>(results: T[]): T[] {
	return results.filter((row) => !row.accepted || waitingUnknown(row));
}

/**
 * Refusals in plain words, each worker named once. Not-sent rows share one
 * reason, so they read "Not sent to A, B: reason." Others read "A: reason".
 * A receipt whose waiting fields are present but null had a metadata failure.
 */
export function receiptText(results: ActionResult[]): string {
	const parts: string[] = [];
	const unsent = new Map<string, string[]>();
	for (const row of results) {
		const reason = (row.reason || 'Refused').replace(/\.$/, '');
		if (notSent(row)) unsent.set(reason, [...(unsent.get(reason) ?? []), row.device_name]);
		else {
			const unknown = waitingUnknown(row);
			parts.push(`${row.device_name}: ${unknown ? `Waiting details unavailable; ${reason}` : reason}`);
		}
	}
	const sent = [...unsent].map(([reason, names]) => `Not sent to ${names.join(', ')}: ${reason}.`);
	return [...sent, parts.join('; ')].filter(Boolean).join(' ');
}

/* ---------- aggregate install outcome ---------- */

export type Outcome = 'refused' | 'partial' | 'complete' | 'failed' | 'waiting';

export interface OperationWorker {
	device_id: string;
	device_name: string;
	status: string;
	message: string;
}

export interface RefusedWorker {
	device_id?: string;
	device_name: string;
	reason: string;
}

export interface UpdateOperation {
	id: string;
	phase: 'waiting' | 'complete' | 'error' | string;
	outcome?: Outcome | string | null;
	refused_workers?: RefusedWorker[] | null;
	completed_at?: number;
	workers: OperationWorker[];
}

/** Workers that were not restarted: the server's list, or skipped rows on an older server. */
export function refusedWorkers(operation: UpdateOperation): RefusedWorker[] {
	if (Array.isArray(operation.refused_workers)) return operation.refused_workers;
	return operation.workers
		.filter((w) => w.status === 'skipped')
		.map((w) => ({ device_id: w.device_id, device_name: w.device_name, reason: w.message || 'Restart not queued' }));
}

/** `outcome` first; an older server's phase otherwise, where skipped is a refusal, not a failed boot. */
export function operationOutcome(operation: UpdateOperation): Outcome {
	const known = ['refused', 'partial', 'complete', 'failed', 'waiting'];
	if (operation.outcome && known.includes(operation.outcome)) return operation.outcome as Outcome;
	const skipped = operation.workers.filter((w) => w.status === 'skipped').length;
	if (operation.workers.length && skipped === operation.workers.length) return 'refused';
	if (operation.phase === 'complete') return skipped ? 'partial' : 'complete';
	return operation.phase === 'error' ? 'failed' : 'waiting';
}

function named(rows: RefusedWorker[]): string {
	return rows.map((w) => `${w.device_name}: ${(w.reason || 'Restart not queued').replace(/\.$/, '')}`).join('; ');
}

export interface OutcomeView {
	outcome: Outcome;
	headline: string;
	/** The full sentence, with each worker named once. */
	detail: string;
	/** Only an all-worker success may pop the completion animation. */
	celebrate: boolean;
	/** Shown as something to look at, not progress. */
	attention: boolean;
}

/** Legacy copy, only for an older server's failed operation without worker errors. */
export const LEGACY_FAILED = 'Update startup could not be confirmed. Check the affected workers before retrying.';

export function outcomeView(operation: UpdateOperation): OutcomeView {
	const outcome = operationOutcome(operation);
	const refused = refusedWorkers(operation);
	switch (outcome) {
		case 'refused':
			return {
				outcome,
				headline: 'Restart was not started',
				detail: `Restart was not started: ${named(refused) || 'no worker accepted the restart'}.`,
				celebrate: false,
				attention: true
			};
		case 'partial':
			return {
				outcome,
				headline: 'Update finished on some workers',
				detail: `Update finished on some workers. Not restarted: ${named(refused)}.`,
				celebrate: false,
				attention: true
			};
		case 'complete':
			return { outcome, headline: 'Update complete', detail: 'Update complete.', celebrate: true, attention: false };
		case 'failed': {
			const failed = operation.workers.filter((w) => w.status === 'error');
			const detail = failed.length
				? failed.map((w) => `${w.device_name}: ${(w.message || 'Update failed').replace(/\.$/, '')}.`).join(' ')
				: operation.outcome
					? 'The update did not finish. Check the affected workers before retrying.'
					: LEGACY_FAILED;
			return { outcome, headline: 'Update needs attention', detail, celebrate: false, attention: true };
		}
		default:
			return {
				outcome: 'waiting',
				headline: 'Waiting on all workers to finish startup',
				detail: refused.length ? `Not restarted: ${named(refused)}.` : '',
				celebrate: false,
				attention: false
			};
	}
}
