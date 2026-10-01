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

/** Why a call cannot start on this session yet, or null when it can. Calls
 * start only on a ready or finished conversation. Busy is the Stop button's
 * signal (streaming, running tasks, has_running_tasks, remote runtime) plus an
 * unanswered permission or question; parked is the sidebar's Parked marker. */
export function callBusyReason(state: CallGateState, row?: CallGateRow | null): string | null {
	const working = hasStoppableWork(state, row ?? undefined)
		|| state.pendingPermissions.length > 0
		|| state.pendingQuestions.length > 0;
	if (working) return CALL_WORKING_REASON;
	if (row?.parked_task_kind) return CALL_PARKED_REASON;
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
