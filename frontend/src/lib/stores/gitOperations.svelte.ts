/**
 * Git Operations Store
 *
 * Tracks the single active git operation surfaced by GitProgressPopover. Two
 * kinds of operation feed this one store:
 *
 * - Client-side chains (Commit / Create PR): the MainPanel handler runs the
 *   sequence itself (generate message → commit → push → open PR) and reports
 *   progress by registering synthetic steps and flipping their status here.
 *
 * - Server-polled operations (Finish / Ship It): the handler kicks off a
 *   backend operation, then this store polls `getGitOperation` every ~1s until
 *   the operation reaches a terminal status, mirroring the server's step list.
 *
 * Either way the store drives `git.setOperationInProgress` (pausing the git
 * poller so it doesn't saturate the browser connection pool), exposes the live
 * step list / status / error / result, and — a few seconds after success —
 * collapses back to idle (success is announced via a toast by the caller). On
 * error it stays put with the failing step's detail until dismissed.
 *
 * Follows the existing store conventions (see git.svelte.ts): $state + the
 * createSubscribers subscribe shim for `$gitOperations` auto-subscription.
 */

import { browser } from '$app/environment';
import * as gitApi from '$lib/api/git';
import { isApiError } from '$lib/api/client';
import { git } from './git.svelte';
import { createSubscribers } from './store-utils';

export type {
	GitOperation,
	GitOperationStep,
	GitOperationResult,
	GitOperationStepStatus
} from '$lib/api/git';

// ============================================================================
// Types
// ============================================================================

/** Popover-facing status. `idle` means no operation is active (popover hidden). */
export type GitOperationsStatus = 'idle' | 'running' | 'done' | 'error';

/** A step registered by a client-side chain before it starts running. */
export interface StepInit {
	id: string;
	label: string;
}

interface GitOperationsState {
	/** Whether an operation is currently active (running, or briefly `done`/`error`). */
	active: boolean;
	status: GitOperationsStatus;
	/** Short label for the operation as a whole (e.g. "Commit", "Ship It"). */
	title: string | null;
	steps: gitApi.GitOperationStep[];
	/** Top-level error message when the operation fails. */
	error: string | null;
	result: gitApi.GitOperationResult | null;
}

const EMPTY_STATE: GitOperationsState = {
	active: false,
	status: 'idle',
	title: null,
	steps: [],
	error: null,
	result: null
};

// Delay before a successful operation collapses the popover back to idle.
const SUCCESS_CLEAR_MS = 2_500;
// Poll cadence for server-side operations.
const POLL_INTERVAL_MS = 1_000;
// How many consecutive poll failures we tolerate before giving up. A single
// transient blip (WS hiccup, momentary 5xx) shouldn't abort an in-flight op.
const MAX_CONSECUTIVE_POLL_FAILURES = 5;
// Terminal cap: the backend keeps an operation for a 10-minute TTL, so anything
// still 'running' past that is effectively lost — stop polling and surface it.
const OPERATION_TIMEOUT_MS = 10 * 60 * 1_000;
// sessionStorage key used to re-attach to a server op across a page refresh.
const PERSIST_KEY = 'ai-shuffle-git-operation';

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<GitOperationsState>({ ...EMPTY_STATE });

const { subscribe, update: _update, set: _set, createDerived } = createSubscribers<GitOperationsState>(
	() => _state,
	(v) => {
		_state = v;
	}
);

// ============================================================================
// Internal (non-reactive) machinery
// ============================================================================

let _clearTimer: ReturnType<typeof setTimeout> | null = null;
// Monotonic token so a stale poll loop / clear timer from a superseded
// operation can't mutate state that belongs to a newer one.
let _opToken = 0;

function _cancelClearTimer(): void {
	if (_clearTimer) {
		clearTimeout(_clearTimer);
		_clearTimer = null;
	}
}

function _delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function _errText(e: unknown): string {
	if (e && typeof e === 'object') {
		const obj = e as { detail?: string; message?: string };
		if (typeof obj.detail === 'string' && obj.detail) return obj.detail;
		if (typeof obj.message === 'string' && obj.message) return obj.message;
	}
	return 'Operation failed';
}

// ── sessionStorage re-attach ────────────────────────────────────────────────
// A server op lives on the backend; if the page refreshes mid-op we'd otherwise
// orphan it (popover/toast lost). We persist the minimal handle while a server
// op is active and clear it on any terminal, then resume polling on store init.

/** The minimal handle needed to re-attach to a server op after a refresh. */
export interface PersistedGitOperation {
	operation_id: string;
	project_id: string;
	session_id: string | null;
	kind: string;
}

function _persistOp(entry: PersistedGitOperation): void {
	if (typeof sessionStorage === 'undefined') return;
	try {
		sessionStorage.setItem(PERSIST_KEY, JSON.stringify(entry));
	} catch {
		/* private-mode / quota — best effort */
	}
}

function _clearPersistedOp(): void {
	if (typeof sessionStorage === 'undefined') return;
	try {
		sessionStorage.removeItem(PERSIST_KEY);
	} catch {
		/* ignore */
	}
}

function _readPersistedOp(): PersistedGitOperation | null {
	if (typeof sessionStorage === 'undefined') return null;
	try {
		const raw = sessionStorage.getItem(PERSIST_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw) as Partial<PersistedGitOperation>;
		if (!parsed || !parsed.operation_id || !parsed.project_id) return null;
		return {
			operation_id: parsed.operation_id,
			project_id: parsed.project_id,
			session_id: parsed.session_id ?? null,
			kind: parsed.kind ?? 'Operation'
		};
	} catch {
		return null;
	}
}

/**
 * Poll a server operation until it reaches a terminal status, mirroring its
 * steps into the store and settling on succeed/fail. Tolerates transient poll
 * errors (only fails after MAX_CONSECUTIVE_POLL_FAILURES in a row, or on a
 * definitive 404), and hard-stops with a timeout error past OPERATION_TIMEOUT_MS.
 * Returns the last-seen snapshot; the caller treats a non-`done` result as a
 * no-op (the failure is already pinned in the popover).
 */
async function _pollToTerminal(
	projectId: string,
	token: number,
	op: gitApi.GitOperation,
	startedAt: number,
	sessionId?: string
): Promise<gitApi.GitOperation> {
	let consecutiveFailures = 0;

	while (op.status === 'running') {
		await _delay(POLL_INTERVAL_MS);
		if (_opToken !== token) return op;

		// Terminal cap — the backend TTL has almost certainly reaped the op.
		if (Date.now() - startedAt >= OPERATION_TIMEOUT_MS) {
			gitOperations.fail('Operation timed out — check the repo state');
			return op;
		}

		try {
			op = await gitApi.getGitOperation(projectId, op.operation_id, undefined, sessionId);
			if (_opToken !== token) return op;
			consecutiveFailures = 0; // any success resets the tolerance window
			gitOperations.replaceSteps(op.steps);
		} catch (e) {
			if (_opToken !== token) return op;
			// A definitive 404 means the op is gone (expired / never existed) —
			// no point retrying, fail immediately.
			if (isApiError(e) && e.status === 404) {
				gitOperations.fail(_errText(e));
				return op;
			}
			// Otherwise treat as transient; only give up after N in a row.
			if (++consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
				gitOperations.fail(_errText(e));
				return op;
			}
		}
	}

	if (op.status === 'done') {
		gitOperations.succeed(op.result ?? null);
	} else {
		const failing = op.steps.find((s) => s.status === 'error');
		gitOperations.fail(failing?.error ?? 'Operation failed', {
			stepId: failing?.id,
			detail: failing?.detail
		});
	}
	return op;
}

// ============================================================================
// Exported Store
// ============================================================================

export const gitOperations = {
	get current() {
		return _state;
	},

	subscribe,

	/**
	 * Begin an operation. Pass the pre-known steps for a client-side chain, or
	 * an empty list for a server-polled operation (its steps arrive from the
	 * first poll). Couples `git.setOperationInProgress(true)` so the git poller
	 * pauses for the duration.
	 *
	 * @returns a token identifying this operation (unused by callers today, but
	 *          lets the store guard against stale async continuations).
	 */
	begin(title: string, steps: StepInit[] = []): number {
		_cancelClearTimer();
		const token = ++_opToken;
		git.setOperationInProgress(true);
		_set({
			active: true,
			status: 'running',
			title,
			steps: steps.map((s) => ({ id: s.id, label: s.label, status: 'pending' as const })),
			error: null,
			result: null
		});
		return token;
	},

	/** Update a single step's status (and optional error/detail). */
	setStep(
		id: string,
		status: gitApi.GitOperationStepStatus,
		patch?: { error?: string; detail?: string }
	): void {
		_update((s) => ({
			...s,
			steps: s.steps.map((step) =>
				step.id === id ? { ...step, status, ...patch } : step
			)
		}));
	},

	/** Replace the whole step list (used when mirroring a server operation). */
	replaceSteps(steps: gitApi.GitOperationStep[]): void {
		_update((s) => ({ ...s, steps }));
	},

	/**
	 * Mark the active operation as succeeded. The popover shows the completed
	 * steps briefly, then collapses to idle. When there are no visible steps it
	 * clears immediately (there's nothing to show).
	 */
	succeed(result?: gitApi.GitOperationResult | null): void {
		git.setOperationInProgress(false);
		_clearPersistedOp();
		const token = _opToken;
		_update((s) => ({
			...s,
			status: 'done',
			result: result ?? s.result
		}));
		if (_state.steps.length === 0) {
			gitOperations.clear();
			return;
		}
		_cancelClearTimer();
		_clearTimer = setTimeout(() => {
			// Only clear if a newer operation hasn't superseded this one.
			if (_opToken === token) gitOperations.clear();
		}, SUCCESS_CLEAR_MS);
	},

	/**
	 * Mark the active operation as failed. Pins the failing step (if any) with
	 * the error detail and keeps the popover open until dismissed.
	 */
	fail(error: string, opts?: { stepId?: string; detail?: string }): void {
		git.setOperationInProgress(false);
		_clearPersistedOp();
		_cancelClearTimer();
		_update((s) => ({
			...s,
			status: 'error',
			error,
			steps: opts?.stepId
				? s.steps.map((step) =>
						step.id === opts.stepId
							? { ...step, status: 'error' as const, error, detail: opts?.detail }
							: step
					)
				: s.steps.map((step) =>
						step.status === 'running'
							? { ...step, status: 'error' as const, error, detail: opts?.detail }
							: step
					)
		}));
	},

	/**
	 * Mark whichever step is currently `running` as failed. Convenience for
	 * client-side chains that track progress positionally rather than by id.
	 */
	failCurrent(error: string, detail?: string): void {
		gitOperations.fail(error, { detail });
	},

	/** Clear the operation immediately (success collapse or error dismiss). */
	clear(): void {
		_cancelClearTimer();
		_clearPersistedOp();
		_opToken++;
		git.setOperationInProgress(false);
		_set({ ...EMPTY_STATE });
	},

	/** Alias for the popover's Dismiss button. */
	dismiss(): void {
		gitOperations.clear();
	},

	/**
	 * Run a server-polled operation: kick it off with `starter`, mirror its
	 * steps, and poll until it reaches a terminal status. Resolves with the
	 * final operation snapshot (or throws if the kickoff/polling errors).
	 */
	async runServer(
		projectId: string,
		title: string,
		starter: () => Promise<gitApi.GitOperation>,
		meta?: { sessionId?: string | null; kind?: string }
	): Promise<gitApi.GitOperation> {
		const token = gitOperations.begin(title, []);
		const startedAt = Date.now();
		try {
			const op = await starter();
			if (_opToken !== token) return op;
			gitOperations.replaceSteps(op.steps);
			// Persist the handle so a mid-op page refresh can re-attach (item 4).
			_persistOp({
				operation_id: op.operation_id,
				project_id: projectId,
				session_id: meta?.sessionId ?? null,
				kind: meta?.kind ?? title
			});
			return await _pollToTerminal(projectId, token, op, startedAt, meta?.sessionId ?? undefined);
		} catch (e) {
			if (_opToken === token) gitOperations.fail(_errText(e));
			throw e;
		}
	},

	/**
	 * Re-attach to a server operation persisted before a page refresh. Reads the
	 * sessionStorage handle, re-opens the popover, and resumes polling so the
	 * op recovers instead of orphaning. No-op when nothing is persisted or an
	 * operation is already active. Fire-and-forget (errors are pinned in the UI).
	 */
	resumePersisted(): void {
		if (_state.active) return; // don't clobber a live operation
		const persisted = _readPersistedOp();
		if (!persisted) return;

		const token = gitOperations.begin(persisted.kind || 'Operation', []);
		const startedAt = Date.now();
		void (async () => {
			try {
				const op = await gitApi.getGitOperation(
					persisted.project_id,
					persisted.operation_id,
					undefined,
					persisted.session_id ?? undefined
				);
				if (_opToken !== token) return;
				gitOperations.replaceSteps(op.steps);
				await _pollToTerminal(persisted.project_id, token, op, startedAt, persisted.session_id ?? undefined);
			} catch (e) {
				if (_opToken === token) gitOperations.fail(_errText(e));
			}
		})();
	}
};

// On store init (app load), re-attach to any operation left mid-flight by a
// page refresh so its popover/toast recovers rather than orphaning the op.
if (browser) {
	gitOperations.resumePersisted();
}

// ============================================================================
// Derived stores
// ============================================================================

/** Whether an operation is actively running (drives button gating). Reactive. */
export const gitOperationRunning = createDerived(() => _state.status === 'running');

/** The full operation state, for the progress popover. */
export const gitOperationState = createDerived(() => _state);
