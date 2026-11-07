/**
 * The chat's red error banner, when a failed turn raised it, belongs to that
 * turn. It is the chat's latest outcome only until the transcript moves past
 * the failure (#1082, #1175):
 *
 *   - a rewind cut the failed turn away, or
 *   - a different turn finished cleanly after it (a queued follow-up, or the
 *     owner's next message), so the latest send no longer reads as failed.
 *
 * Both are read from the event log, never from when the socket's `error` frame
 * happened to arrive: that frame can reach the browser before or after the
 * feed's own events, and a reload or resume replays the whole log afterwards.
 * The log records each failure as `turn.aborted(reason='error', message)`
 * (the reducer's `turnFailures`), with the same message the frame carries. So
 * the banner is tied to that durable failure: by the frame's `turn_id` when it
 * has one, else by its message.
 *
 * When the frame arrives, `afterSeq` is the seq of the newest failure already in
 * the log that it could NOT be for (see `turnErrorAnchor`); the banner then
 * belongs to the newest matching failure after that, whenever it is reduced.
 * Until that failure is in the log the banner stays: a pre-stream refusal that
 * never becomes a turn keeps its old behaviour.
 *
 * Only the exact banner the anchor was taken for is cleared: any other message
 * written into `error` since (a take-over, a refused send) keeps its own rules.
 */
import type { TurnFailureFact } from './v2/reducer';

/** What ties a banner to the failed turn that raised it. */
export interface TurnErrorAnchor {
	/** The banner text this anchor belongs to. */
	message: string;
	/** The frame's own turn id, when it named one. */
	turnId: string | null;
	/** The banner belongs to a matching failure with a seq above this one. */
	afterSeq: number;
}

/** The feed facts the rule reads (see V2SessionState). */
export interface TurnOutcomeFacts {
	turnFailures?: readonly TurnFailureFact[];
}

function belongsTo(anchor: Pick<TurnErrorAnchor, 'message' | 'turnId'>, failure: TurnFailureFact): boolean {
	return anchor.turnId ? failure.turnId === anchor.turnId : failure.message === anchor.message;
}

function newestMatch(anchor: Pick<TurnErrorAnchor, 'message' | 'turnId'>, failures: readonly TurnFailureFact[], afterSeq = -1): TurnFailureFact | null {
	for (let i = failures.length - 1; i >= 0; i--) {
		const f = failures[i];
		if (f.seq <= afterSeq) return null;
		if (belongsTo(anchor, f)) return f;
	}
	return null;
}

/**
 * Tie a newly raised banner to its failure, or return 'outlived' when the
 * frame came late: its failure is already in the log and already outlived.
 *
 * `turnRunning` is whether the chat shows a turn in progress. A frame named by
 * text alone whose newest matching failure is already outlived can be one of
 * two things: the late frame of that old failure, or the early frame of a new
 * run that failed with the same words before the feed delivered its abort. A
 * running turn means the second, so the banner waits for a failure after the
 * old one; otherwise it is the first.
 */
export function turnErrorAnchor(
	message: string,
	turnId: string | null,
	facts: TurnOutcomeFacts,
	turnRunning: boolean
): TurnErrorAnchor | 'outlived' | null {
	if (!message) return null;
	const failures = facts.turnFailures ?? [];
	const known = newestMatch({ message, turnId }, failures);
	if (!known) return { message, turnId, afterSeq: -1 };
	if (!known.outlived) return { message, turnId, afterSeq: known.seq - 1 };
	if (turnId || !turnRunning) return 'outlived';
	return { message, turnId, afterSeq: known.seq };
}

/** Whether the banner `anchor` was taken for has been outlived by the transcript. */
export function turnErrorOutlived(anchor: TurnErrorAnchor | null | undefined, error: string | null, facts: TurnOutcomeFacts): boolean {
	if (!anchor || error !== anchor.message) return false;
	return newestMatch(anchor, facts.turnFailures ?? [], anchor.afterSeq)?.outlived === true;
}
