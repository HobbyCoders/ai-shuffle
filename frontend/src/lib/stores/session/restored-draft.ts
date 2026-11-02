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
