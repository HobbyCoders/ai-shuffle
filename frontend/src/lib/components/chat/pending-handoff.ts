/**
 * A hand-off the agent queued with HandOffSession, read from the transcript.
 *
 * The owner's own move is tracked by the runner chip itself while its request
 * runs. A hand-off the agent queues runs later, on the owning worker, when the
 * turn and its background work end (app/core/session_handoff.py). The chat
 * already carries every step of it: the tool's result (`queued: true` and the
 * worker's label), then `handoff` rows recording `started`, and finally
 * `moved`, `failed` or `timeout`. A later call with `cancel` drops the request.
 *
 * Those records form one episode per hand-off (handoff-episodes.ts), which the
 * transcript shows as one row; this reads the episode still open. A queued request survives a restart of the owning worker,
 * which resumes it; one it cannot resume ends with a `cancelled` or `timeout`
 * row, so the chat never shows a queued move that no longer exists.
 *
 * The owner's "Move when this turn ends" (#1819) queues the same hand-off on
 * the owning worker, which records a `queued` row (`after: 'queued'` when
 * queued messages run first); cancelling it records `cancelled`. Both read the
 * same way here, whoever queued the move.
 */
import type { ChatMessage } from '$lib/stores/types';
import type { QueuedMoveBusy } from '$lib/api/sessions';
import { handoffEpisodes, isHandoffRow, isHandoffTool, settleByOwner, type HandoffOwner } from '$lib/utils/handoff-episodes';

export interface PendingHandoff {
	/** The target worker's label, as the tool reported it. */
	worker: string;
	/** 'queued': waiting for the turn to end. 'moving': commit, push and move running. */
	phase: 'queued' | 'moving';
	/** Queued messages run before the move (#1819). */
	afterQueued?: boolean;
}

/** True when a hand-off row or HandOffSession call follows index `from`. */
export function handoffActivitySince(messages: readonly ChatMessage[], from: number): boolean {
	return messages.slice(from).some((m) => isHandoffRow(m) || isHandoffTool(m));
}

/**
 * The hand-off still waiting or running. Read from the same episodes the
 * transcript row renders (handoff-episodes.ts), so the chip, the composer and
 * the row always agree: 'queued' is the row's "Starting", 'moving' its "Sending".
 * A call still waiting for its result has queued nothing yet. `owner` is the
 * chat's session row: once it names the target, the move is done here even
 * though this worker never sees the `moved` record.
 */
export function pendingHandoff(messages: readonly ChatMessage[], owner?: HandoffOwner | null): PendingHandoff | null {
	const open = settleByOwner(handoffEpisodes(messages), owner).open;
	if (!open || open.requesting || !open.worker) return null;
	if (open.phase === 'sending') return { worker: open.worker, phase: 'moving' };
	return open.afterQueued ? { worker: open.worker, phase: 'queued', afterQueued: true } : { worker: open.worker, phase: 'queued' };
}

/**
 * The composer's placeholder while a move waits or runs (#1819). Messages sent
 * meanwhile are held and sent on the new worker after the move.
 */
export function handoffPlaceholder(handoff: PendingHandoff | null): string | null {
	if (!handoff) return null;
	if (handoff.phase === 'moving') return `Sending to ${handoff.worker}…`;
	return handoff.afterQueued
		? `Moves to ${handoff.worker} after the queued messages`
		: `Moves to ${handoff.worker} after this turn`;
}

/** Whether `busy` keeps the chat from moving now: it then queues the move. */
export function moveBlocked(busy: QueuedMoveBusy): boolean {
	return busy.running || busy.tasks.length > 0 || !!busy.wake;
}
