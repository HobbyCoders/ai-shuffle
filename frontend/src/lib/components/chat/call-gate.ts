import type { Session } from '$lib/api/sessions';
import type { Task } from '$lib/stores/types';
import { notify } from '$lib/stores/notifications';

export interface CallGateState {
	sessionId: string | null;
	isStreaming: boolean;
	activeTasks: Task[];
	pendingPermissions: readonly unknown[];
	pendingQuestions: readonly unknown[];
}

type CallGateRow = Pick<Session, 'id' | 'is_remote' | 'controllable' | 'remote_runtime_state' | 'has_running_tasks' | 'parked_task_kind'> & {
	device_name?: string | null;
};

/** Why a call cannot reach a conversation whose worker is offline. */
export function ownerOfflineReason(deviceName?: string | null): string {
	return `${deviceName?.trim() || 'The worker that runs this conversation'} is offline, so a call could not reach its chat`;
}

/** Why a call cannot start on this session, or null when it can.
 *
 * Every conversation can be called at any time: while it works, while it
 * waits on background jobs and while a decision is pending. The call reads
 * the conversation's status (`check_work`) and asks its approvals aloud.
 * One another worker runs takes a call too (#700): the call runs on this
 * worker and forwards to that one, so it only needs that worker online.
 * `state` is kept so callers pass one shape. */
export function callBusyReason(state: CallGateState, row?: CallGateRow | null): string | null {
	if (!row?.is_remote || row.id !== state.sessionId) return null;
	return row.controllable === false ? ownerOfflineReason(row.device_name) : null;
}

export const CALL_CREATING_REASON = 'Calls can start once this session is created';

/** Why the new-session composer cannot start a call, or null when it can. The
 * call creates the session first, on the "Runs on" worker when one is picked
 * (#700); the call then runs here and forwards to it. The create refuses an
 * offline or ineligible pick itself, with the reason shown where a failed
 * first send shows it. */
export function freshCallReason(state: {
	isStreaming: boolean;
	isCreatingWorktree: boolean;
}): string | null {
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
