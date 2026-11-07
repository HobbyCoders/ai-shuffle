/**
 * The chat's red error banner, when a failed turn raised it, belongs to that
 * turn. It is the chat's latest outcome only until the transcript moves past
 * the failure (#1082, #1175):
 *
 *   - a rewind cut the failed turn away, or
 *   - a different turn finished cleanly after it (a queued follow-up, or the
 *     owner's next message), so the latest send no longer reads as failed.
 *
 * Both are read from the event log, in log order, never from when the
 * socket's `error` frame happened to arrive: that frame usually reaches the
 * browser before its turn's failure is logged, a lagging or hydrating feed can
 * be far behind it, and a reload or reconnect replays the log afterwards.
 *
 * The runtime names the failed turn on the frame (`turn_id`, the id on that
 * turn's `turn.aborted` / `turn.completed`, #1238), and that is the only tie.
 * The feed controller holds the anchor and steps it through every event it
 * reduces (`followTurnError`), not only at a fold, so a replay folded in
 * batches, or in one, ends where the log says:
 *
 *   1. waiting: until the log records the end of the frame's turn. Nothing
 *      before that end can retire the banner.
 *   2. bound to that end's seq. A clean finish of another turn, or a rewind
 *      that cuts at or before it, logged after it, retires the banner.
 *   3. outlived: the next fold clears the banner.
 *
 * A frame that arrives after its turn's failure was reduced binds to it at
 * once from the reducer's failure index (`TurnOutcomes.failures`, the last
 * TURN_FAILURES_KEPT failed turns); a frame for a failure the log has already
 * moved past raises nothing. A frame for a turn older than the whole index
 * finds nothing and waits, so it stays until sent or dismissed.
 *
 * A frame with no `turn_id` (an older worker, or an error that is not a turn)
 * is never retired by the log: its words could belong to any failure, and
 * guessing from them can clear the current failure on an older one's
 * successor. It stays until the owner sends or dismisses it, as every banner
 * did before this change.
 *
 * Only the exact banner the anchor was taken for is cleared: any other message
 * written into `error` since (a take-over, a refused send) keeps its own rules.
 */
import {
	completedCleanly,
	failureOutlived,
	NO_TURN_OUTCOMES,
	type TurnOutcomes,
	type WireEvent,
} from './v2/reducer';

/** What ties a banner to the failed turn that raised it. */
export interface TurnErrorAnchor {
	/** The banner text this anchor belongs to. */
	message: string;
	/** The frame's turn id. */
	turnId: string;
	/** The logged end of that turn, once the feed reduced it. */
	failure: { seq: number; turnId: string } | null;
	/** The log has moved past `failure`: the next fold clears the banner. */
	outlived: boolean;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/**
 * Tie a banner the socket just raised to its failed turn. Null when there is
 * nothing to follow (no message, or the frame names no turn); 'outlived' when
 * the frame names a turn whose failure the log has already moved past: it
 * came late, and raises nothing.
 */
export function raiseTurnError(message: string, turnId: string | null, outcomes: TurnOutcomes = NO_TURN_OUTCOMES): TurnErrorAnchor | 'outlived' | null {
	if (!message || !turnId) return null;
	const anchor: TurnErrorAnchor = { message, turnId, failure: null, outlived: false };
	const known = outcomes.failures.find((f) => f.turnId === turnId);
	// Not logged yet (the usual case): followed event by event from here.
	if (!known) return anchor;
	return failureOutlived(outcomes, known) ? 'outlived' : { ...anchor, failure: { seq: known.seq, turnId } };
}

/** Step the anchor through one reduced log event; the same object when it
 *  changes nothing. */
export function followTurnError(anchor: TurnErrorAnchor, event: WireEvent): TurnErrorAnchor {
	if (anchor.outlived) return anchor;
	const seq = typeof event.seq === 'number' ? event.seq : null;
	if (seq === null) return anchor;
	if (!anchor.failure) {
		const ended = event.type === 'turn.aborted' || event.type === 'turn.completed';
		return ended && str(event.turn_id) === anchor.turnId ? { ...anchor, failure: { seq, turnId: anchor.turnId } } : anchor;
	}
	if (seq <= anchor.failure.seq) return anchor;
	const finishedAfter = event.type === 'turn.completed' && str(event.turn_id) !== anchor.failure.turnId
		&& completedCleanly(event as Record<string, unknown>);
	const cutAway = event.type === 'transcript.rewound' && typeof event.from_seq === 'number'
		&& event.from_seq <= anchor.failure.seq;
	return finishedAfter || cutAway ? { ...anchor, outlived: true } : anchor;
}

/** A replaced log (the feed restarted on it) means old seqs say nothing: the
 *  anchor waits for its turn's end again, unless it is already outlived. */
export function unplaceTurnError(anchor: TurnErrorAnchor): TurnErrorAnchor {
	return anchor.outlived ? anchor : { ...anchor, failure: null };
}
