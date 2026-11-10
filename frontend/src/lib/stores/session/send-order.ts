/**
 * Send order — sends to one chat are written in the order they were made
 * (#1444), whichever component made them (the composer, the Browser panel's
 * element prompt, a generation choice, a review comment).
 *
 * A send takes its turn the moment it is made. If an earlier send to the same
 * chat has not yet written its frame (it is waiting for the socket, a restore,
 * a voice call's settings save or a note's ownership answer), the later one
 * waits until that frame is written or that send ends without one. A send
 * made while a reload is still restoring the chat cannot know its chat yet,
 * so it waits for, and holds, every send around it.
 *
 * Plain .ts file — rune-free, like message-sender.ts.
 */

/** The chat a send was made in while the store did not know it yet. */
export const ANY_CHAT: unique symbol = Symbol('any chat');

/** A session id, null for a new chat's compose, or ANY_CHAT. */
export type SendChat = string | null | typeof ANY_CHAT;

export interface SendTurn {
	/** Settles once every earlier send to this chat has let it go; null when
	 *  there is none, so the send goes on without an await. */
	ready: Promise<void> | null;
	/** Lets later sends to this chat go. Idempotent. */
	release: () => void;
}

export function createSendOrder() {
	type OpenTurn = { chat: SendChat; done: Promise<void> };
	const open: OpenTurn[] = [];
	return {
		/** Take a turn now, in the order sends are made. */
		take(chat: SendChat): SendTurn {
			const earlier = open.filter(turn => turn.chat === chat || turn.chat === ANY_CHAT || chat === ANY_CHAT);
			let finish!: () => void;
			const entry: OpenTurn = { chat, done: new Promise<void>(resolve => { finish = resolve; }) };
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
	};
}
