/**
 * Where a message is meant to go, and what happens to it when the user has
 * moved on before it went out (#1077).
 *
 * A send can await before the message leaves: the composer's cold-cache
 * check, and the store's wait for a reloaded chat's restore. The store sends
 * to whichever chat is open when it gets there, so New chat or a chat switch
 * in that window used to carry the message into the view the user moved to.
 *
 * One rule for every send path: capture the destination when the user sends,
 * re-check it after each await (the store re-checks it after its restore wait,
 * see `stillAtDestination` in message-sender), and when it no longer holds,
 * keep the words as the draft of the chat they were typed in and say so.
 */
import { activeSession } from '$lib/stores/active-session';
import { getInitGeneration } from '$lib/stores/session/session-init';
import { writeDraft } from '$lib/stores/session/draft-persistence';
import { hasComposerDraft } from '$lib/stores/session/composer-suggestion';
import { notify } from '$lib/stores/notifications';
import { joinDrafts } from './tiptap/serialization';

/** The composer's place right now: the open chat and the chat its editor holds. */
export interface ComposerPlace {
	sessionId: string | null;
	editorOwner: string | null;
}

export interface SendDestination {
	/** The chat the message is for; null for New chat. During a reload's
	 *  restore the store has no chat yet, so it is the chat the editor holds. */
	readonly chat: string | null;
	readonly navigation: number;
	/** The leave count when the user sent (see noteLeftDraft). */
	readonly leaves: number;
	/** Send order, so called-off messages are kept in the order they were sent. */
	readonly order: number;
}

export const SEND_CALLED_OFF_MESSAGE = 'Message not sent: the chat changed before it went out.';
export const SEND_MOVED_MESSAGE =
	`${SEND_CALLED_OFF_MESSAGE} It is kept as a draft in the chat you wrote it in.`;

function chatOf(place: ComposerPlace): string | null {
	return place.sessionId ?? place.editorOwner;
}

/** The latest document each chat's editor held when the user left it. */
const leftDrafts = new Map<string | null, { draft: string; at: number }>();
let leaves = 0;
const LEFT_DRAFTS_KEPT = 16;

/**
 * The composer is leaving `chat`: remember what its editor held. A send
 * called off afterwards keeps this, the newest text, rather than the words
 * as they were at Send. Recorded even when the composer skips its own save.
 */
export function noteLeftDraft(chat: string | null, draft: string): void {
	leftDrafts.delete(chat);
	leftDrafts.set(chat, { draft, at: ++leaves });
	if (leftDrafts.size > LEFT_DRAFTS_KEPT) leftDrafts.delete(leftDrafts.keys().next().value!);
}

let sendOrder = 0;

export function captureDestination(place: ComposerPlace): SendDestination {
	return { chat: chatOf(place), navigation: getInitGeneration(), leaves, order: ++sendOrder };
}

/**
 * True while a send may still go to its destination: no navigation since
 * the user sent, and the same chat open. `adoptedOwnChat` admits a New chat
 * that has since taken the session its own send created (#1636).
 */
export function stillAtDestination(
	destination: SendDestination,
	place: ComposerPlace,
	options: { adoptedOwnChat?: boolean } = {}
): boolean {
	if (getInitGeneration() !== destination.navigation) return false;
	if (chatOf(place) === destination.chat) return true;
	return !!options.adoptedOwnChat && destination.chat === null;
}

/** True when the composer now shows another chat than the destination. */
export function movedAwayFrom(destination: SendDestination, place: ComposerPlace): boolean {
	if (chatOf(place) === destination.chat) return false;
	return !stillAtDestination(destination, place, { adoptedOwnChat: true });
}

/** What the chat's editor held when the user left it after this send, if seen. */
function laterThan(destination: SendDestination): string | null {
	const left = leftDrafts.get(destination.chat);
	return left && left.at > destination.leaves ? left.draft : null;
}

/**
 * The draft to keep for the destination chat once its send is called off.
 * `cleared`: the composer had already emptied for the send, so what it held
 * when the user left is a later message to keep after the unsent one; left
 * empty, the unsent words alone. Otherwise the composer still held the words
 * when the user left, edits included, and that document wins over the text as
 * it was at Send, even when the user emptied it.
 */
export function draftToKeep(destination: SendDestination, unsent: string, cleared: boolean): string {
	const later = laterThan(destination);
	if (!cleared) return later ?? unsent;
	return later && hasComposerDraft(later) ? joinDrafts(unsent, later) : unsent;
}

/**
 * Called-off sends for one chat since the user last left it. Several can be
 * outstanding at once (Send, Send again once the composer cleared, then New
 * chat while a reload restores), and each is kept, in send order, ahead of
 * what the composer held.
 */
interface KeptForChat {
	leave: number;
	unsent: { order: number; draft: string }[];
	/** The composer's document, from a send called off before it cleared. */
	held: string | null;
}
const kept = new Map<string | null, KeptForChat>();

function keptFor(chat: string | null): KeptForChat {
	const leave = leftDrafts.get(chat)?.at ?? 0;
	const current = kept.get(chat);
	if (current?.leave === leave) return current;
	const fresh: KeptForChat = { leave, unsent: [], held: null };
	kept.delete(chat);
	kept.set(chat, fresh);
	if (kept.size > LEFT_DRAFTS_KEPT) kept.delete(kept.keys().next().value!);
	return fresh;
}

/**
 * Keep a called-off send as its chat's draft (store and reload mirror) and
 * say so. The composer has moved to another chat, whose own draft, New chat
 * included, is left alone.
 */
export function keepUnsentDraft(destination: SendDestination, unsent: string, cleared: boolean): void {
	const entry = keptFor(destination.chat);
	if (cleared) {
		entry.unsent.push({ order: destination.order, draft: unsent });
		entry.unsent.sort((a, b) => a.order - b.order);
	} else {
		entry.held = draftToKeep(destination, unsent, false);
	}
	const tail = entry.held ?? laterThan(destination);
	const parts = [...entry.unsent.map(u => u.draft), ...(tail && hasComposerDraft(tail) ? [tail] : [])];
	const draft = parts.length ? parts.reduce(joinDrafts) : '';
	activeSession.saveDraftForSession(destination.chat, draft);
	writeDraft(destination.chat, draft, { preserveNewChat: true });
	notify.banner(parts.length ? SEND_MOVED_MESSAGE : SEND_CALLED_OFF_MESSAGE, { severity: 'warning', persist: false, duration_ms: 6000 });
}
