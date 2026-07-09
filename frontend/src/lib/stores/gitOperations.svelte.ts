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

import * as gitApi from '$lib/api/git';
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
		starter: () => Promise<gitApi.GitOperation>
	): Promise<gitApi.GitOperation> {
		const token = gitOperations.begin(title, []);
		try {
			let op = await starter();
			if (_opToken !== token) return op;
			gitOperations.replaceSteps(op.steps);

			while (op.status === 'running') {
				await _delay(POLL_INTERVAL_MS);
				if (_opToken !== token) return op;
				op = await gitApi.getGitOperation(projectId, op.operation_id);
				if (_opToken !== token) return op;
				gitOperations.replaceSteps(op.steps);
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
		} catch (e) {
			if (_opToken === token) gitOperations.fail(_errText(e));
			throw e;
		}
	}
};

// ============================================================================
// Derived stores
// ============================================================================

/** Whether an operation is actively running (drives button gating). Reactive. */
export const gitOperationRunning = createDerived(() => _state.status === 'running');

/** The full operation state, for the progress popover. */
export const gitOperationState = createDerived(() => _state);
