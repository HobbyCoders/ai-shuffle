/**
 * Keep a stopped message in its bubble until the stopped turn has settled.
 *
 * Stop turns queued messages into "Not sent" rows right away, but the turn it
 * stopped settles a moment later and its reply grows (the [Stopped] marker and
 * the work summary). A row is taller than its bubble and sits below that reply,
 * so showing rows early moves them, and any bubbles under them, while the person
 * is reading. Instead each stays a bubble of the same size until the turn ends,
 * then they all change in the one update that ends it, or, when a message sent
 * after Stop is waiting below them, when that message leaves the queue. The row is
 * already in the transcript's data, so a reload while the turn still runs replays it and shows
 * the same bubbles. Once the turn has ended a reload shows rows at once.
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
	let lastQueued: QueuedMessage[] | null = null;
	let heldFor: string | null = null;
	let last: NotSentHold = { messages: [], bubbles: NOTHING_HELD };
	return (messages: ChatMessage[], liveTurnId: string | null | undefined, queued: QueuedMessage[] = NOTHING_HELD): NotSentHold => {
		if (messages !== lastMessages || liveTurnId !== lastTurnId || queued !== lastQueued) {
			// A message sent after Stop is still waiting below these when the turn
			// ends. Changing them now would push it down the screen, with its own
			// turn about to replace it anyway, so they wait for it. A hold only ever
			// continues: rows already showing never go back to being bubbles.
			const carriedOver = !liveTurnId && heldFor !== null && queued.some(q => !last.bubbles.some(b => b.id === q.id));
			const turn = liveTurnId ?? (carriedOver ? heldFor : null);
			last = holdNotSentRows(messages, turn);
			heldFor = last.bubbles.length > 0 ? turn ?? null : null;
			lastMessages = messages;
			lastTurnId = liveTurnId;
			lastQueued = queued;
		}
		return last;
	};
}

/** What a Stop pressed before the feed named its turn is recorded against. */
export const STOP_TURN_NOT_NAMED = 'turn-not-named';

/**
 * Whether the page is still waiting to hear how this device's Stop ended. The turn
 * id, not streaming: Stop's timeout ends streaming on this page without the feed
 * having said anything, and the rows are still on their way.
 */
export function stopPressedOn(requested: string | null | undefined, liveTurnId: string | null | undefined): boolean {
	return requested === STOP_TURN_NOT_NAMED || (!!liveTurnId && requested === liveTurnId);
}

/** The feed names the turn a Stop was for, or ends it. A later turn was never stopped. */
export function nameStoppedTurn(
	requested: string | null | undefined,
	liveTurnId: string | null,
	streaming: boolean
): string | null | undefined {
	if (requested !== STOP_TURN_NOT_NAMED) return requested;
	if (!streaming) return null;
	return liveTurnId ?? STOP_TURN_NOT_NAMED;
}

/** Bind an unconfirmed Stop's recovery even if it timed out before the feed named it.
 * Non-streaming folds are not confirmation: delayed rows/start may still arrive.
 * A genuinely different live turn ends recovery; it was never part of this Stop.
 */
export function nameStopRecovery(
	timedOut: string | null | undefined,
	liveTurnId: string | null
): string | null {
	if (!timedOut) return null;
	if (timedOut === STOP_TURN_NOT_NAMED) return liveTurnId ?? timedOut;
	return liveTurnId && liveTurnId !== timedOut ? null : timedOut;
}

/** Also releases a carried hold after settlement, when there is no live turn. */
export function stopRecoveryAppliesToTurn(
	timedOut: string | null | undefined,
	liveTurnId: string | null | undefined
): boolean {
	return !!timedOut && (!liveTurnId || stopPressedOn(timedOut, liveTurnId));
}

/** The chat ends on words Stop refused: nothing below them is coming. */
export function restingOnNotSentRows(messages: ChatMessage[]): boolean {
	return messages.findLast(m => m.role === 'user')?.metadata?.not_sent === true;
}

/**
 * True on the one update where a chat already on screen comes to rest on not-sent
 * rows. Opening a chat that rests on them is not an arrival: that reader asked to
 * see the end of it.
 */
export function createNotSentRowsArrival() {
	let lastSession: string | null | undefined;
	let wasResting = false;
	let hadMessages = false;
	return (sessionId: string | null | undefined, messages: ChatMessage[]): boolean => {
		const resting = restingOnNotSentRows(messages);
		const arrived = sessionId === lastSession && hadMessages && !wasResting && resting;
		lastSession = sessionId;
		wasResting = resting;
		hadMessages = messages.length > 0;
		return arrived;
	};
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
 * their rows appear, so what the queue showed when this device pressed Stop stays
 * until its row is held. Nothing else can get that label: a message sent after
 * Stop was not there. A row the transcript shows delivered ends the wait too. The
 * stopped turn's end arrives after its rows on the same stream, so nothing
 * outlives the turn.
 */
export function createStopBubbleHold() {
	let beforeStop: QueuedMessage[] = NOTHING_HELD;
	let kept: QueuedMessage[] | null = null;
	let lastSession: string | null | undefined;
	return (
		sessionId: string | null | undefined,
		bubbles: QueuedMessage[],
		stopPressed: boolean,
		messages: ChatMessage[]
	): QueuedMessage[] => {
		// One MessageArea serves every chat: another chat's bubbles are not missing here.
		if (sessionId !== lastSession) {
			beforeStop = NOTHING_HELD;
			kept = null;
		}
		lastSession = sessionId;
		if (!stopPressed) {
			beforeStop = bubbles;
			kept = null;
			return bubbles;
		}
		kept ??= beforeStop.filter(isWaiting);
		// Withdrawn after Stop: the queue taking it away is what the person asked for.
		const withdrawn = bubbles.filter(m => !isWaiting(m) && m.status !== 'stopped');
		kept = kept.filter(m => !withdrawn.some(b => isSameBubble(b, m)));

		const unclaimed = [...bubbles];
		let missing = kept.filter(m => {
			const answer = unclaimed.findIndex(b => isSameBubble(b, m));
			if (answer >= 0) unclaimed.splice(answer, 1);
			return answer < 0;
		});
		if (missing.length === 0) return bubbles;

		const inTranscript = new Set(
			messages.filter(m => m.role === 'user').flatMap(m => [m.queuedTurnId, m.turnId, m.commandId])
		);
		const hasRow = (m: QueuedMessage) => inTranscript.has(m.id) || (!!m.clientMessageId && inTranscript.has(m.clientMessageId));
		kept = kept.filter(m => !hasRow(m));
		missing = missing.filter(m => !hasRow(m));
		if (missing.length === 0) return bubbles;
		return [
			...bubbles.filter(m => m.status === 'stopped'),
			...missing.map(m => ({ ...m, status: 'stopped' as const })),
			...bubbles.filter(m => m.status !== 'stopped'),
		];
	};
}

function isWaiting(m: QueuedMessage): boolean {
	return !m.status;
}

/** A send the server has not named yet can only be told from its row by its words. */
function isSameBubble(bubble: QueuedMessage, kept: QueuedMessage): boolean {
	return bubble.id === kept.id
		|| (!!kept.clientMessageId && bubble.clientMessageId === kept.clientMessageId)
		|| (kept.id === kept.clientMessageId && bubble.content === kept.content);
}
