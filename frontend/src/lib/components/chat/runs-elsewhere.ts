import type { ActiveSessionState } from '$lib/stores/active-session';

/**
 * Whether the composer's chat runs, or will run, on a worker other than the
 * one serving this page. A note picked from `@` is then inserted as its id
 * rather than this worker's file path (#1364).
 *
 * - An open chat: the session row's owner, else the owner `/live` reported.
 *   When neither is known yet the answer is "elsewhere": the id resolves on
 *   any worker, while this worker's path is only right for its own chats.
 * - A new chat: the "Runs on" pick (`deviceIntent`; `null` is this device).
 */
export function chatRunsElsewhere(
	state: Pick<ActiveSessionState, 'sessionId' | 'isRemote' | 'deviceIntent'>,
	row?: { is_remote?: boolean | null } | null
): boolean {
	if (!state.sessionId) return !!state.deviceIntent?.id;
	return (row?.is_remote ?? state.isRemote) !== false;
}
