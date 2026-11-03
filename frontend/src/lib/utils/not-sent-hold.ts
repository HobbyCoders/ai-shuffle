/**
 * Keep a stopped message in its bubble until the stopped turn has settled.
 *
 * Stop turns queued messages into "Not sent" rows right away, but the turn it
 * stopped settles a moment later and its reply grows (the [Stopped] marker and
 * the work summary). A row is taller than its bubble and sits below that reply,
 * so showing rows early moves them, and any bubbles under them, while the person
 * is reading. Instead each stays a bubble of the same size until the turn ends,
 * then they all change in the one update that ends it. The row is already in the
 * transcript's data, so a reload during the wait shows the same thing.
 */
import type { ChatMessage, QueuedMessage } from '$lib/stores/types';

export interface NotSentHold {
	/** The transcript without held rows; the input array itself when nothing is held. */
	messages: ChatMessage[];
	/** Held rows as bubbles, in transcript order. */
	bubbles: QueuedMessage[];
}

const NOTHING_HELD: QueuedMessage[] = [];

export function holdNotSentRows(messages: ChatMessage[], liveTurnId: string | null | undefined): NotSentHold {
	if (!liveTurnId) return { messages, bubbles: NOTHING_HELD };
	const isHeld = (m: ChatMessage) => m.metadata?.not_sent === true && m.metadata.not_sent_during_turn === liveTurnId;
	if (!messages.some(isHeld)) return { messages, bubbles: NOTHING_HELD };
	return {
		messages: messages.filter(m => !isHeld(m)),
		bubbles: messages.filter(isHeld).map(m => ({
			// A queued row's id, so a late queue snapshot cannot add a second copy.
			id: m.queuedTurnId ?? m.turnId ?? m.id,
			content: m.content,
			queuedAt: 0,
			status: 'stopped' as const,
		})),
	};
}

/** The same transcript array for the same input, so a held row does not rebuild the transcript on every store update. */
export function createNotSentHold() {
	let lastMessages: ChatMessage[] | null = null;
	let lastTurnId: string | null | undefined;
	let last: NotSentHold = { messages: [], bubbles: NOTHING_HELD };
	return (messages: ChatMessage[], liveTurnId: string | null | undefined): NotSentHold => {
		if (messages !== lastMessages || liveTurnId !== lastTurnId) {
			last = holdNotSentRows(messages, liveTurnId);
			lastMessages = messages;
			lastTurnId = liveTurnId;
		}
		return last;
	};
}

/**
 * True once a stopped turn has handed its rows to the transcript and is waiting
 * out the settle hold. The settle pin then decides where the reader rests, so
 * rows taller than the room left must not scroll them down first: they would
 * follow on the hand-over and be pulled back up a second later.
 */
export function restingOnNotSentRows(
	messages: ChatMessage[],
	liveTurnId: string | null | undefined,
	turnUnsettled: boolean
): boolean {
	if (liveTurnId || !turnUnsettled) return false;
	return messages.findLast(m => m.role === 'user')?.metadata?.not_sent === true;
}

/** Held bubbles first (they left the queue's front), without a copy from a late queue snapshot. */
export function withHeldBubbles(held: QueuedMessage[], queued: QueuedMessage[]): QueuedMessage[] {
	if (held.length === 0) return queued;
	const heldIds = new Set(held.map(m => m.id));
	return [...held, ...queued.filter(m => !heldIds.has(m.id))];
}

/**
 * Stop's queue snapshot and its rows take separate routes to the page, and the
 * snapshot can land first. Left alone the bubbles would vanish a moment before
 * their rows appear, so after this device presses Stop a bubble the snapshot
 * took away stays until its row is held. The stopped turn's end arrives after
 * its rows on the same stream, so nothing outlives the turn.
 */
export function createStopBubbleHold() {
	let shown: QueuedMessage[] = NOTHING_HELD;
	let shownSession: string | null | undefined;
	return (sessionId: string | null | undefined, bubbles: QueuedMessage[], stopPressed: boolean): QueuedMessage[] => {
		// One MessageArea serves every chat: another chat's bubbles are not missing here.
		if (sessionId !== shownSession) shown = NOTHING_HELD;
		shownSession = sessionId;
		const missing = !stopPressed ? NOTHING_HELD : shown.filter(
			m => m.status !== 'cancelling' && m.status !== 'delivered' && !bubbles.some(b => isSameBubble(b, m))
		);
		shown = missing.length === 0 ? bubbles : [
			...bubbles.filter(m => m.status === 'stopped'),
			...missing.map(m => ({ ...m, status: 'stopped' as const })),
			...bubbles.filter(m => m.status !== 'stopped'),
		];
		return shown;
	};
}

/** A send the server has not named yet can only be told from its row by its words. */
function isSameBubble(bubble: QueuedMessage, missing: QueuedMessage): boolean {
	return bubble.id === missing.id || (missing.id === missing.clientMessageId && bubble.content === missing.content);
}
