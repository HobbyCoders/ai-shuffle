/**
 * The chat's red error banner, when a failed turn raised it, belongs to that
 * turn. It is the chat's latest outcome only until the transcript moves past
 * the failure (#1082, #1175):
 *
 *   - a rewind after the banner went up cut the failed turn away (rewinds run
 *     only between turns, so the cut always reaches the latest ended turn), or
 *   - a different turn finished cleanly after it (a queued follow-up, or the
 *     owner's next message), so the latest send no longer reads as failed.
 *
 * The socket's `error` frame names no turn and can reach the browser before or
 * after the feed's own turn events, so the banner is anchored to where the feed
 * stood when it went up (`seq`) plus the turn that was live then, if any.
 * Only the exact banner the anchor was taken for is cleared: any other message
 * written into `error` since (a take-over, a refused send) keeps its own rules.
 */

/** Where the feed stood when a failed turn raised the banner. */
export interface TurnErrorAnchor {
	/** The banner text this anchor belongs to. */
	message: string;
	/** The feed's last reduced seq when the banner went up. */
	seq: number;
	/** The turn that was live then (null when the feed had not seen it start, or had already seen it end). */
	turnId: string | null;
}

/** The feed facts the rule reads (see V2SessionState). */
export interface TurnOutcomeFacts {
	lastCompletedTurn?: { id: string; seq: number } | null;
	lastRewindSeq?: number;
}

export function turnErrorAnchor(message: string, feedSeq: number | null, liveTurnId: string | null): TurnErrorAnchor | null {
	return message && feedSeq !== null ? { message, seq: feedSeq, turnId: liveTurnId } : null;
}

/** Whether the banner `anchor` was taken for has been outlived by the transcript. */
export function turnErrorOutlived(anchor: TurnErrorAnchor | null | undefined, error: string | null, facts: TurnOutcomeFacts): boolean {
	if (!anchor || error !== anchor.message) return false;
	if ((facts.lastRewindSeq ?? 0) > anchor.seq) return true;
	const done = facts.lastCompletedTurn;
	return !!done && done.seq > anchor.seq && done.id !== anchor.turnId;
}
