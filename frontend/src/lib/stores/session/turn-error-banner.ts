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
 * feed's own events, and a reload or resume replays the log afterwards. The log
 * records each failure as `turn.aborted(reason='error', message)`, with the
 * same message the frame carries (the reducer's `turnOutcomes`). So the banner
 * follows that durable failure:
 *
 *   - A frame that names its turn (`turn_id`) belongs to that turn's failure,
 *     whenever the feed reduces it. That is exact at any point of a replay.
 *   - A frame named by its message alone could be for any failure with the
 *     same words, so it is only matched against a log that is complete: the
 *     feed has caught up (hydrated, and not replaying a reconnect's gap). Until
 *     then the banner stays and nothing about it is decided, because a partly
 *     replayed log can hold an old failure with these words and its successor
 *     but not yet the failure this frame is for. Once caught up, the banner
 *     follows the newest failure with its words that the log has not moved
 *     past. When there is none, its failure is still on the way, so it follows
 *     the first one logged after that point.
 *
 * Whether a failure is outlived is read from facts the reducer keeps for every
 * failure ever logged (`failureOutlived`), so a failure that has dropped out of
 * the reducer's short list of recent failures still retires its banner.
 *
 * A message-only frame whose failure never shows up after that point was a
 * late repeat of an old one. Its banner stays until the next clean finish or
 * rewind is logged, which it could only precede if it were already logged.
 * A pre-stream refusal that never becomes a turn keeps its old behaviour.
 *
 * Only the exact banner the anchor was taken for is cleared: any other message
 * written into `error` since (a take-over, a refused send) keeps its own rules.
 */
import { failureOutlived, NO_TURN_OUTCOMES, type TurnFailureFact, type TurnOutcomes } from './v2/reducer';

/** What ties a banner to the failed turn that raised it. */
export interface TurnErrorAnchor {
	/** The banner text this anchor belongs to. */
	message: string;
	/** The frame's own turn id, when it named one. */
	turnId: string | null;
	/** The logged failure the banner follows, once known. */
	failure: { seq: number; turnId: string } | null;
	/** With no failure yet: only a failure logged after this seq can be the
	 *  banner's. Null until a message-only frame has been matched against a
	 *  caught-up log. */
	afterSeq: number | null;
}

/** The feed facts the rule reads. */
export interface TurnErrorFacts {
	outcomes?: TurnOutcomes;
	/** The seq the feed has reduced up to. */
	lastSeq?: number;
	/** Whether the reduced log is the whole log as of now: hydration finished on
	 *  the server's sentinel, and no reconnect is replaying a gap. */
	caughtUp?: boolean;
}

function belongsTo(anchor: TurnErrorAnchor, failure: TurnFailureFact): boolean {
	return anchor.turnId ? failure.turnId === anchor.turnId : failure.message === anchor.message;
}

function newestMatch(anchor: TurnErrorAnchor, failures: readonly TurnFailureFact[], afterSeq: number): TurnFailureFact | null {
	for (let i = failures.length - 1; i >= 0; i--) {
		const f = failures[i];
		if (f.seq <= afterSeq) return null;
		if (belongsTo(anchor, f)) return f;
	}
	return null;
}

/**
 * Tie a banner the socket just raised to its failure. 'outlived' when the frame
 * names a turn whose failure the log has already moved past: it came late.
 */
export function raiseTurnError(message: string, turnId: string | null, facts: TurnErrorFacts): TurnErrorAnchor | 'outlived' | null {
	if (!message) return null;
	return followTurnError({ message, turnId, failure: null, afterSeq: null }, message, facts);
}

/**
 * The banner's anchor after the feed reduced more of the log: the same object
 * when nothing changed, a new one when it found or moved to its failure, or
 * 'outlived' once the log has moved past that failure and the banner goes.
 * `error` is the banner shown now; an anchor taken for other text is left be.
 */
export function followTurnError(
	anchor: TurnErrorAnchor | null | undefined,
	error: string | null,
	facts: TurnErrorFacts
): TurnErrorAnchor | 'outlived' | null {
	if (!anchor || error !== anchor.message) return anchor ?? null;
	// Message-only: nothing is decided against a log still catching up.
	if (!anchor.turnId && !facts.caughtUp) return anchor;
	const outcomes = facts.outcomes ?? NO_TURN_OUTCOMES;
	let a = anchor;
	if (!a.failure && a.afterSeq === null) {
		const known = newestMatch(a, outcomes.failures, -1);
		if (known && !failureOutlived(outcomes, known)) {
			a = { ...a, failure: { seq: known.seq, turnId: known.turnId } };
		} else if (a.turnId) {
			// Its own failure, already outlived: a late frame. Not logged yet: wait.
			return known ? 'outlived' : a;
		} else {
			// Every failure with these words is behind us: this one is still on the way.
			a = { ...a, afterSeq: facts.lastSeq ?? 0 };
		}
	}
	// A newer failure with the same words (or of the same turn) is the one the
	// banner now reports.
	const newer = newestMatch(a, outcomes.failures, a.failure?.seq ?? a.afterSeq ?? -1);
	if (newer) a = { ...a, failure: { seq: newer.seq, turnId: newer.turnId } };
	if (a.failure) return failureOutlived(outcomes, a.failure) ? 'outlived' : a;
	if (a.turnId || a.afterSeq === null) return a;
	// Still no failure since `afterSeq`, yet a clean finish or a rewind was
	// logged after it: the frame's failure came before that point after all.
	const latestFinish = outcomes.finishes[0]?.seq ?? 0;
	return latestFinish > a.afterSeq || outcomes.lastRewindSeq > a.afterSeq ? 'outlived' : a;
}
