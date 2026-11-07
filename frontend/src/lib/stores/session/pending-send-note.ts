/**
 * A message whose Send is still waiting to go out (#933).
 *
 * Send clears the composer and its saved draft at once, then the cold-cache
 * check may hold the message before it is handed to the store. A browser
 * refresh in that window would leave nothing holding the words, so the send
 * notes its document here first and drops the note the moment the message is
 * handed over or put back. A later page puts any note it finds back in front
 * of the chat's draft.
 *
 * The key starts with the draft prefix, so an account change clears these
 * notes with the drafts (session-owner.ts).
 */
import { joinDrafts, restoreMessageDocument } from '$lib/components/chat/tiptap/serialization';
import { hasComposerDraft } from './composer-suggestion';

const PREFIX = '__aihub_draft_pending-send:';
const THIS_PAGE = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
let count = 0;

interface PendingSendNote {
	doc: string;
	uploadPaths: string[];
	page: string;
	at: number;
}

const chatPrefix = (chat: string) => `${PREFIX}${chat}:`;

/** Note a send's words before it waits. Returns the note's key, or null. */
export function notePendingSend(chat: string | null, doc: string, uploadPaths: string[] = []): string | null {
	if (!chat || (!hasComposerDraft(doc) && uploadPaths.length === 0)) return null;
	const key = `${chatPrefix(chat)}${THIS_PAGE}:${++count}`;
	try {
		const note: PendingSendNote = { doc, uploadPaths, page: THIS_PAGE, at: Date.now() * 1000 + count };
		sessionStorage.setItem(key, JSON.stringify(note));
		return key;
	} catch {
		return null; // Storage is a safety net: the send itself still runs.
	}
}

/** The message was handed over or put back: forget its note. */
export function dropPendingSend(key: string | null): void {
	if (!key) return;
	try { sessionStorage.removeItem(key); } catch { /* See notePendingSend. */ }
}

/**
 * Words an earlier page was still holding for `chat` when it went away, in the
 * order they were sent, as one draft document; '' when there are none. The
 * notes are removed. This page's own notes belong to sends still waiting.
 */
export function takePendingSends(chat: string, page: string = THIS_PAGE): string {
	const notes: PendingSendNote[] = [];
	try {
		const prefix = chatPrefix(chat);
		for (const key of Object.keys(sessionStorage)) {
			if (!key.startsWith(prefix)) continue;
			let note: PendingSendNote | null = null;
			try { note = JSON.parse(sessionStorage.getItem(key) ?? 'null'); } catch { /* unreadable */ }
			if (note?.page === page) continue;
			sessionStorage.removeItem(key);
			if (note && typeof note.doc === 'string') notes.push(note);
		}
	} catch { /* See notePendingSend. */ }
	notes.sort((a, b) => a.at - b.at);
	let draft = '';
	for (const note of notes) {
		const paths = Array.isArray(note.uploadPaths) ? note.uploadPaths : [];
		const words = paths.length
			? (hasComposerDraft(note.doc) ? joinDrafts(note.doc, JSON.stringify(restoreMessageDocument('', paths))) : JSON.stringify(restoreMessageDocument('', paths)))
			: note.doc;
		draft = hasComposerDraft(draft) ? joinDrafts(draft, words) : words;
	}
	return draft;
}
