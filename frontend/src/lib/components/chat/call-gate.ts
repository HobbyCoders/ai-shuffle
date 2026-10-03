import type { Session } from '$lib/api/sessions';
import type { Task } from '$lib/stores/types';
import { notify } from '$lib/stores/notifications';
import { hasStoppableWork } from './stoppable-work';

export const CALL_WORKING_REASON = 'Calls can start once this session finishes working';
export const CALL_PARKED_REASON = 'Calls can start once this session’s background tasks finish';

export interface CallGateState {
	sessionId: string | null;
	isStreaming: boolean;
	activeTasks: Task[];
	pendingPermissions: readonly unknown[];
	pendingQuestions: readonly unknown[];
}

type CallGateRow = Pick<Session, 'id' | 'is_remote' | 'controllable' | 'remote_runtime_state' | 'has_running_tasks' | 'parked_task_kind'>;

export const CALL_ELSEWHERE_REASON = 'Calls run on the device that runs this conversation';

export interface CallGateOptions {
	/** The persistent assistant's home conversation. It can be called at any
	 * time: while it works, while it waits on background jobs and while a
	 * decision is pending, because the call reads its status and asks its
	 * approvals aloud. */
	alwaysAvailable?: boolean;
}

/** Why a call cannot start on this session yet, or null when it can.
 *
 * An ordinary conversation takes a call only when it is ready or finished
 * (#1611). Busy is the Stop button's signal (streaming, running tasks,
 * has_running_tasks, remote runtime) plus an unanswered permission or
 * question; parked is the sidebar's Parked marker.
 *
 * The assistant's home is always callable, except when another device runs
 * it: a call attaches only to a conversation this install runs. */
export function callBusyReason(
	state: CallGateState,
	row?: CallGateRow | null,
	options: CallGateOptions = {}
): string | null {
	if (options.alwaysAvailable) {
		return row?.is_remote && row.id === state.sessionId ? CALL_ELSEWHERE_REASON : null;
	}
	const working = hasStoppableWork(state, row ?? undefined)
		|| state.pendingPermissions.length > 0
		|| state.pendingQuestions.length > 0;
	if (working) return CALL_WORKING_REASON;
	if (row?.parked_task_kind) return CALL_PARKED_REASON;
	return null;
}

export const CALL_CREATING_REASON = 'Calls can start once this session is created';

/** Why the new-session composer cannot start a call, or null when it can. The
 * call creates the session first, on this install: a call only attaches to a
 * conversation owned here, so a picked runner on another device blocks it. */
export function freshCallReason(state: {
	isStreaming: boolean;
	isCreatingWorktree: boolean;
	deviceIntent: { name: string } | null;
}): string | null {
	if (state.deviceIntent) return `Calls run on this device. Switch the runner to Local to call, or send a message to start on ${state.deviceIntent.name}`;
	// A first message is already creating the session.
	if (state.isStreaming || state.isCreatingWorktree) return CALL_CREATING_REASON;
	return null;
}

/** Touch screens have no hover, so a disabled button's tooltip never shows.
 * There the button stays tappable (aria-disabled) and a tap explains itself. */
export function prefersTouch(): boolean {
	return typeof window !== 'undefined'
		&& typeof window.matchMedia === 'function'
		&& window.matchMedia('(hover: none)').matches;
}

let lastExplanation: string | null = null;

/** Show why a call cannot start as a brief, self-dismissing banner. A repeat
 * tap replaces the previous one instead of stacking. */
export function explainBlockedCall(reason: string): void {
	if (lastExplanation) notify.dismissLive(lastExplanation);
	lastExplanation = notify.banner(reason, { severity: 'info', persist: false, duration_ms: 3000 });
}
