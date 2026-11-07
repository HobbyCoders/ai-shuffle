/**
 * Restored words for a chat that is not on screen.
 *
 * Restoring a "Not sent" row can finish after the owner has moved to another
 * chat. By then the server has dropped the row, so the words go straight into
 * that chat's stored draft: in front of anything already there, and into both
 * places a draft is read from when the chat opens again. Upload chips travel
 * inside the draft document.
 */
import { restoreMessageDocument, joinDrafts } from '$lib/components/chat/tiptap/serialization';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { hasComposerDraft } from './composer-suggestion';
import { readDraft, writeDraft } from './draft-persistence';

export function addRestoredWordsToDraft(sessionId: string, prompt: string, uploadPaths: string[] = [], openDraft = ''): string {
	// Same preference as opening the chat: the open chat's draft, then memory,
	// then the reload mirror.
	const existing = openDraft || sessionEphemeralCache.peekDraft(sessionId) || readDraft(sessionId) || '';
	if (!prompt && uploadPaths.length === 0) return existing;
	const restored = JSON.stringify(restoreMessageDocument(prompt, uploadPaths));
	const draft = hasComposerDraft(existing) ? joinDrafts(restored, existing) : restored;
	sessionEphemeralCache.patchDraft(sessionId, draft);
	writeDraft(sessionId, draft);
	return draft;
}

/**
 * Restores a page did not live to finish.
 *
 * The server can drop a "Not sent" row while its answer is still on the way. If
 * the page reloads in that gap, nothing is left that holds the words, so each
 * restore notes them in this tab before it asks. A page only reads notes left
 * by an earlier page: its own restores still have their answers coming.
 */
export interface UnfinishedRestore {
	rowId: string;
	prompt: string;
	uploadPaths: string[];
	composerAtConfirm?: string | null;
	page: string;
}

const THIS_PAGE = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const noteKey = (sessionId: string, rowId: string) => `restore-note:${sessionId}:${rowId}`;

// A reload rejects the requests it cuts off, so a lost answer can mean the page
// is going away rather than a bad connection. Its composer goes with it.
export const LEAVING_MS = 1000;
let leaving = false;
if (typeof window !== 'undefined') {
	const markLeaving = () => {
		leaving = true;
		// Still here: the departure was cancelled, or the page came back from cache.
		setTimeout(() => { leaving = false; }, LEAVING_MS);
	};
	window.addEventListener('beforeunload', markLeaving);
	window.addEventListener('pagehide', markLeaving);
}
export const pageIsLeaving = () => leaving;

export function noteRestoreStarted(sessionId: string, rowId: string, prompt: string, uploadPaths: string[] = [], composerAtConfirm: string | null = null): void {
	try {
		const note: UnfinishedRestore = { rowId, prompt, uploadPaths, composerAtConfirm, page: THIS_PAGE };
		sessionStorage.setItem(noteKey(sessionId, rowId), JSON.stringify(note));
	} catch { /* Storage is a safety net: the restore itself still runs. */ }
}

export function noteRestoreFinished(sessionId: string, rowId: string): void {
	try {
		sessionStorage.removeItem(noteKey(sessionId, rowId));
	} catch { /* See noteRestoreStarted. */ }
}

/**
 * A rewound SENT message's note key (#1083). Its row id is not stable across a
 * reload (an optimistic id on the sending page, the event id after), so it is
 * named the way the rewind itself finds it: the prompt's text and which
 * occurrence of that text it is among the chat's prompts.
 */
export function sentRewindRowId(content: string, occurrence: number): string {
	const text = content.trim();
	let hash = 5381;
	for (let i = 0; i < text.length; i++) hash = ((hash * 33) ^ text.charCodeAt(i)) >>> 0;
	return `sent:${occurrence}:${text.length}:${hash.toString(36)}`;
}

/** The sent-message note keys of the prompts a transcript still shows. */
export function sentRowIdsOnScreen(messages: ReadonlyArray<{ role?: string; type?: string; content?: string }>): string[] {
	const seen = new Map<string, number>();
	const ids: string[] = [];
	for (const m of messages) {
		if (m.role !== 'user' || m.type === 'command') continue;
		const text = (m.content ?? '').trim();
		const occurrence = seen.get(text) ?? 0;
		seen.set(text, occurrence + 1);
		ids.push(sentRewindRowId(text, occurrence));
	}
	return ids;
}

/**
 * Notes from an earlier page whose row has gone: the server did drop it, and the
 * words were never handed back. A row still on screen keeps its note, because
 * the request may yet land; the chat's next opening looks again.
 */
export function takeUnfinishedRestores(sessionId: string, rowIdsOnScreen: Set<string>, page: string = THIS_PAGE): UnfinishedRestore[] {
	const found: UnfinishedRestore[] = [];
	try {
		const prefix = noteKey(sessionId, '');
		for (const key of Object.keys(sessionStorage)) {
			if (!key.startsWith(prefix)) continue;
			const note = JSON.parse(sessionStorage.getItem(key) ?? 'null') as UnfinishedRestore | null;
			if (!note || note.page === page || rowIdsOnScreen.has(note.rowId)) continue;
			sessionStorage.removeItem(key);
			found.push(note);
		}
	} catch { /* See noteRestoreStarted. */ }
	return found;
}
