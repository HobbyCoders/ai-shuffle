/**
 * Send order — sends to one chat are written in the order they were made
 * (#1444), whichever component made them (the composer, the Browser panel's
 * element prompt, a generation choice, Resolve with agent, a review comment).
 *
 * One rule:
 *  - Every send belongs to one lane: the chat it was made in, or, for a new
 *    chat's compose, that compose. A chat created from the compose stays in
 *    the compose's lane while a send of it is still open.
 *  - A send goes only on its place in line, taken the moment it is made,
 *    before any await, and used once. A caller that awaits before it sends
 *    (the composer, a generation choice) reserves it at the click
 *    (`reserveSend`). A cancelled or used place sends nothing.
 *  - A send queued in a new chat's compose reads, when its turn comes, how
 *    the chat creation ahead of it ended: it goes to the chat created, or is
 *    refused if that creation failed. Never a second create.
 *  - A turn ends only on a known outcome: its frame was written or admitted,
 *    the chat it creates was adopted, it was refused, or it was cancelled or
 *    abandoned. Elapsed time never ends a turn.
 *  - A later send never overtakes: if an earlier turn has not ended within a
 *    bounded wait, the later send is refused and its caller keeps the words.
 *
 * A send made while a reload is still restoring the chat cannot know its lane
 * yet, so it waits for, and holds, every send around it (ANY_CHAT).
 *
 * Plain .ts file — rune-free, like message-sender.ts.
 */

/** The lane of a send made while the store did not know its chat yet. */
export const ANY_CHAT: unique symbol = Symbol('any chat');

/** `chat:<session id>`, `compose:<navigation>` or ANY_CHAT. */
export type SendLane = string | typeof ANY_CHAT;

export interface SendTurn {
	/** Settles once every earlier turn in this lane has ended; null when
	 *  there is none, so the send goes on without an await. */
	ready: Promise<void> | null;
	/** Ends this turn. Idempotent. */
	release: () => void;
}

/** A place in line taken before the send is made (`reserveSend`). */
export interface SendReservation {
	/** Gives the place up: the send it was for then sends nothing.
	 *  Idempotent; a no-op once the send was made with it. */
	cancel(): void;
}

export function createSendOrder() {
	type OpenTurn = { lane: SendLane; done: Promise<void> };
	const open: OpenTurn[] = [];
	const meets = (a: SendLane, b: SendLane) => a === b || a === ANY_CHAT || b === ANY_CHAT;
	return {
		/** Take a turn now, in the order sends are made. */
		take(lane: SendLane): SendTurn {
			const earlier = open.filter(turn => meets(turn.lane, lane));
			let finish!: () => void;
			const entry: OpenTurn = { lane, done: new Promise<void>(resolve => { finish = resolve; }) };
			open.push(entry);
			let released = false;
			return {
				ready: earlier.length ? Promise.all(earlier.map(turn => turn.done)).then(() => {}) : null,
				release() {
					if (released) return;
					released = true;
					open.splice(open.indexOf(entry), 1);
					finish();
				},
			};
		},
		/** Whether a turn in this lane has not ended yet. */
		isOpen(lane: SendLane): boolean {
			return open.some(turn => turn.lane === lane);
		},
	};
}
