/** What a commit showed: whose chat, how many transcript rows, how many queued bubbles. */
export interface QueueHandoffMark {
	sessionId: string | null | undefined;
	messages: number;
	queued: number;
}

/**
 * Whether a queued prompt's row landed in the same commit its bubble left
 * (#1052). The transcript grows and the queue shrinks together, so the view
 * must hold the painted queue slot for that one paint. A withdrawal, a plain
 * reply, a fresh send or a chat switch is not a handoff.
 */
export function landedFromQueue(previous: QueueHandoffMark | null, next: QueueHandoffMark): boolean {
	return !!previous && previous.sessionId === next.sessionId
		&& next.messages > previous.messages && next.queued < previous.queued;
}
