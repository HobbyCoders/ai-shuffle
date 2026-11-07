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
 * turn's `turn.aborted` / `turn.completed`, #1238), and that is the only exact
 * tie. The feed controller holds the anchor and steps it through every event
 * it reduces (`followTurnError`), not only at a fold, so a replay folded in
 * batches, or in one, ends where the log says:
 *
 *   1. waiting: until the log records the end of the frame's turn. Nothing
 *      before that end can retire the banner, whatever its words.
 *   2. bound to that end's seq. A clean finish of another turn, or a rewind
 *      that cuts at or before it, logged after it, retires the banner.
 *   3. outlived: the next fold clears the banner.
 *
 * A frame that arrives after its turn's failure was reduced binds to it at
 * once from the reducer's recent failures; a frame for a failure the log has
 * already moved past raises nothing.
 *
 * A frame with no `turn_id` comes from an older worker (or is not about a
 * turn). Its words can match any failure, so it is followed conservatively:
 * it never clears for anything logged before a failure with the same words
 * that was logged after the frame arrived. "Arrived" is the feed's position
 * then, once that position is the end of a complete log: during hydration or
 * a reconnect's replay it is the server's `caught_up` point instead. A frame
 * whose failure the feed had already passed (or one that never becomes a
 * turn) is never matched, and stays until the owner sends or dismisses it, as
 * every banner did before. The one residual: a feed that lags the socket by a
 * whole earlier failed turn with the very same words can bind to that one.
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
	/** The frame's own turn id, when it named one. */
	turnId: string | null;
	/** The logged end of the banner's failed turn, once the feed reduced it. */
	failure: { seq: number; turnId: string } | null;
	/** A message-only frame: only a failure logged after this seq can be its
	 *  own. Null until that position is known (see `placeTurnError`). */
	afterSeq: number | null;
	/** The log has moved past `failure`: the next fold clears the banner. */
	outlived: boolean;
}

/** The feed facts read when a frame arrives. */
export interface TurnErrorFacts {
	outcomes?: TurnOutcomes;
	/** The seq the feed has reduced up to. */
	lastSeq?: number;
	/** The reduced log is the whole log as of the last `caught_up`: hydration
	 *  ended on the server's sentinel and no reconnect is replaying a gap. */
	caughtUp?: boolean;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/**
 * Tie a banner the socket just raised to its failure. 'outlived' when the
 * frame names a turn whose failure the log has already moved past: it came
 * late, and raises nothing.
 */
export function raiseTurnError(message: string, turnId: string | null, facts: TurnErrorFacts): TurnErrorAnchor | 'outlived' | null {
	if (!message) return null;
	const anchor: TurnErrorAnchor = { message, turnId, failure: null, afterSeq: null, outlived: false };
	if (!turnId) return placeTurnError(anchor, facts);
	const outcomes = facts.outcomes ?? NO_TURN_OUTCOMES;
	const known = outcomes.failures.findLast((f) => f.turnId === turnId);
	// Not logged yet (the usual case): followed event by event from here.
	if (!known) return anchor;
	return failureOutlived(outcomes, known) ? 'outlived' : { ...anchor, failure: { seq: known.seq, turnId } };
}

/**
 * Give a message-only anchor its arrival position, once the reduced log is
 * complete: at the frame when the feed was caught up, else at the next
 * `caught_up` sentinel. The same anchor when there is nothing to place.
 */
export function placeTurnError(anchor: TurnErrorAnchor, facts: TurnErrorFacts): TurnErrorAnchor {
	if (anchor.turnId || anchor.failure || anchor.afterSeq !== null || !facts.caughtUp) return anchor;
	return { ...anchor, afterSeq: facts.lastSeq ?? 0 };
}

/** Step the anchor through one reduced log event; the same object when it
 *  changes nothing. */
export function followTurnError(anchor: TurnErrorAnchor, event: WireEvent): TurnErrorAnchor {
	if (anchor.outlived) return anchor;
	const seq = typeof event.seq === 'number' ? event.seq : null;
	if (seq === null) return anchor;
	const ended = event.type === 'turn.aborted' || event.type === 'turn.completed';
	if (!anchor.failure) {
		if (!ended) return anchor;
		const turnId = str(event.turn_id);
		const own = anchor.turnId
			? turnId === anchor.turnId
			: anchor.afterSeq !== null && seq > anchor.afterSeq && event.type === 'turn.aborted'
				&& event.reason === 'error' && str(event.message) === anchor.message;
		return own ? { ...anchor, failure: { seq, turnId } } : anchor;
	}
	if (seq <= anchor.failure.seq) return anchor;
	const finishedAfter = event.type === 'turn.completed' && str(event.turn_id) !== anchor.failure.turnId
		&& completedCleanly(event as Record<string, unknown>);
	const cutAway = event.type === 'transcript.rewound' && typeof event.from_seq === 'number'
		&& event.from_seq <= anchor.failure.seq;
	return finishedAfter || cutAway ? { ...anchor, outlived: true } : anchor;
}

/** A replaced log (the feed restarted on it) means old seqs say nothing: the
 *  anchor waits for its failure again, unless it is already outlived. */
export function unplaceTurnError(anchor: TurnErrorAnchor): TurnErrorAnchor {
	return anchor.outlived ? anchor : { ...anchor, failure: null, afterSeq: null };
}
