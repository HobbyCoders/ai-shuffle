import type { ActiveSessionState } from '$lib/stores/active-session';
import type { SessionOwnership } from '$lib/stores/app-data.svelte';

/**
 * Whether a message sent now from the composer runs on a worker other than
 * the one serving this page. A note chip is then sent as `@Notes/<id>` rather
 * than this worker's file path (#1364). Asked at send time, so a "Runs on" or
 * owner change after the note was picked counts.
 *
 * - An open chat: the session row's owner, else the owner `/live` reported.
 *   When neither is known the answer is "elsewhere": the id resolves on any
 *   worker, while this worker's path is only right for its own chats.
 * - `owners`: a "sessions" signal (a hand-off, a reconnect) the session list
 *   has not answered yet leaves the owner unknown. Managed-account and paired
 *   sockets learn of a hand-off only this way. Once it answers, a chat with no
 *   row there is still unknown: `/live` may have spoken before the signal.
 * - `moving`: a queued move holds the message and sends it to the new worker.
 * - A new chat: the "Runs on" pick (`deviceIntent`; `null` is this device).
 */
export function chatRunsElsewhere(
	state: Pick<ActiveSessionState, 'sessionId' | 'isRemote' | 'deviceIntent'>,
	row?: { is_remote?: boolean | null } | null,
	moving = false,
	owners: SessionOwnership = { signals: 0, answered: 0 }
): boolean {
	if (!state.sessionId) return !!state.deviceIntent?.id;
	if (moving || owners.answered < owners.signals || (owners.signals > 0 && !row)) return true;
	return (row?.is_remote ?? state.isRemote) !== false;
}
