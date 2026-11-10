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
import { get } from 'svelte/store';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
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
 * Called-off sends whose words were put back into a chat's draft, in send
 * order. Several can be outstanding at once (Send, Send again once the
 * composer cleared, then a timeout or New chat), and they can come back at
 * different times, with the chat revisited in between. Each one put back
 * takes its send-order place among those still at the head of the draft,
 * ahead of the text the user typed later. Sending the composer retires them
 * (retirePutBack); one edited away is no longer matched and drops out.
 */
interface PutBack {
	order: number;
	blocks: unknown[];
	keys: string[];
}
const putBack = new Map<string | null, PutBack[]>();

/** A stable key for a document block, whatever its property order. */
function blockKey(block: unknown): string {
	const sorted = (value: unknown): unknown =>
		Array.isArray(value) ? value.map(sorted)
		: value && typeof value === 'object'
			? Object.fromEntries(Object.keys(value).sort().map(key => [key, sorted((value as Record<string, unknown>)[key])]))
			: value;
	return JSON.stringify(sorted(block));
}

function docOf(draft: string | null): { type: 'doc'; content?: unknown[] } | null {
	if (!draft) return { type: 'doc', content: [] };
	try {
		const doc = JSON.parse(draft);
		return doc?.type === 'doc' ? doc : null;
	} catch {
		return null;
	}
}

/**
 * Put a called-off send's words back into `current`, the chat's draft now,
 * in send order among the earlier ones put back, and return the result.
 */
export function putBackUnsent(destination: SendDestination, unsent: string, current: string | null): string {
	const chat = destination.chat;
	const unsentDoc = docOf(unsent);
	const currentDoc = docOf(current);
	if (!unsentDoc || !currentDoc) {
		putBack.delete(chat);
		return current && hasComposerDraft(current) ? joinDrafts(unsent, current) : unsent;
	}
	const blocks = currentDoc.content ?? [];
	const keys = blocks.map(blockKey);
	// The earlier sends put back that still lead the draft, untouched.
	const still: PutBack[] = [];
	let at = 0;
	for (const entry of putBack.get(chat) ?? []) {
		const n = entry.keys.length;
		if (n && entry.keys.every((key, k) => keys[at + k] === key)) {
			still.push(entry);
			at += n;
		}
	}
	const rest = blocks.slice(at);
	const unsentBlocks = unsentDoc.content ?? [];
	const entries = hasComposerDraft(unsent)
		? [...still, { order: destination.order, blocks: unsentBlocks, keys: unsentBlocks.map(blockKey) }].sort((a, b) => a.order - b.order)
		: still;
	putBack.delete(chat);
	putBack.set(chat, entries);
	if (putBack.size > LEFT_DRAFTS_KEPT) putBack.delete(putBack.keys().next().value!);
	const tail = hasComposerDraft(JSON.stringify({ type: 'doc', content: rest })) ? rest : [];
	const content = [...entries.flatMap(entry => entry.blocks), ...tail];
	return content.length ? JSON.stringify({ ...unsentDoc, content }) : '';
}

/** The composer is sending this chat's draft: the words put back go with it. */
export function retirePutBack(chat: string | null): void {
	putBack.delete(chat);
}

/** Composers on screen, newest last, by the chat each one shows. */
const composers: { chat: () => string | null; take: (unsent: string, destination: SendDestination) => void }[] = [];

/** A composer is on screen; the returned function takes it off. */
export function mountComposer(composer: (typeof composers)[number]): () => void {
	composers.push(composer);
	return () => {
		const at = composers.indexOf(composer);
		if (at >= 0) composers.splice(at, 1);
	};
}

/**
 * A send outlived its composer and was refused: its words go back in front
 * of what the composer now showing its chat holds, or into that chat's draft
 * for the next composer to open with. Never dropped (#1444).
 */
export function handBackUnsent(destination: SendDestination, unsent: string): void {
	for (let at = composers.length - 1; at >= 0; at--) {
		if (composers[at].chat() === destination.chat) return composers[at].take(unsent, destination);
	}
	const state = get(activeSession);
	const open = state.sessionId === destination.chat;
	const draft = putBackUnsent(destination, unsent, (open ? state.draft : '') || readDraft(destination.chat));
	if (open) activeSession.setDraft(draft);
	else activeSession.saveDraftForSession(destination.chat, draft);
	writeDraft(destination.chat, draft, { preserveNewChat: true });
}

/**
 * Keep a called-off send as its chat's draft (store and reload mirror) and
 * say so. The composer has moved to another chat, whose own draft, New chat
 * included, is left alone.
 */
export function keepUnsentDraft(destination: SendDestination, unsent: string, cleared: boolean): void {
	const left = leftDrafts.get(destination.chat);
	const later = laterThan(destination);
	const draft = cleared ? putBackUnsent(destination, unsent, later) : draftToKeep(destination, unsent, false);
	// A send called off after this one builds on this draft, not the one left.
	if (left && later !== null) left.draft = draft;
	const kept = hasComposerDraft(draft);
	activeSession.saveDraftForSession(destination.chat, kept ? draft : '');
	writeDraft(destination.chat, kept ? draft : '', { preserveNewChat: true });
	notify.banner(kept ? SEND_MOVED_MESSAGE : SEND_CALLED_OFF_MESSAGE, { severity: 'warning', persist: false, duration_ms: 6000 });
}
