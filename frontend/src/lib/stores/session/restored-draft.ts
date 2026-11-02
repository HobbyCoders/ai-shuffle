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

export function addRestoredWordsToDraft(sessionId: string, prompt: string, uploadPaths: string[] = []): void {
	if (!prompt && uploadPaths.length === 0) return;
	const restored = JSON.stringify(restoreMessageDocument(prompt, uploadPaths));
	// Same preference as opening the chat: memory first, then the reload mirror.
	const existing = sessionEphemeralCache.peekDraft(sessionId) || readDraft(sessionId) || '';
	const draft = hasComposerDraft(existing) ? joinDrafts(restored, existing) : restored;
	sessionEphemeralCache.patchDraft(sessionId, draft);
	writeDraft(sessionId, draft);
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
	page: string;
}

const THIS_PAGE = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const noteKey = (sessionId: string, rowId: string) => `restore-note:${sessionId}:${rowId}`;

// A reload rejects the requests it cuts off, so a lost answer can mean the page
// is going away rather than a bad connection. Its composer goes with it.
let leaving = false;
if (typeof window !== 'undefined') {
	const markLeaving = () => {
		leaving = true;
		// Still here: the departure was cancelled, or the page came back from cache.
		setTimeout(() => { leaving = false; }, 1000);
	};
	window.addEventListener('beforeunload', markLeaving);
	window.addEventListener('pagehide', markLeaving);
}
export const pageIsLeaving = () => leaving;

export function noteRestoreStarted(sessionId: string, rowId: string, prompt: string, uploadPaths: string[] = []): void {
	try {
		const note: UnfinishedRestore = { rowId, prompt, uploadPaths, page: THIS_PAGE };
		sessionStorage.setItem(noteKey(sessionId, rowId), JSON.stringify(note));
	} catch { /* Storage is a safety net: the restore itself still runs. */ }
}

export function noteRestoreFinished(sessionId: string, rowId: string): void {
	try {
		sessionStorage.removeItem(noteKey(sessionId, rowId));
	} catch { /* See noteRestoreStarted. */ }
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
