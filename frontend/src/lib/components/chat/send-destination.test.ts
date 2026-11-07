import { beforeEach, describe, expect, it, vi } from 'vitest';
import { bumpInitGeneration } from '$lib/stores/session/session-init';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
import { notify } from '$lib/stores/notifications';
import {
	captureDestination, draftToKeep, keepUnsentDraft, movedAwayFrom, noteLeftDraft, SEND_MOVED_MESSAGE, stillAtDestination
} from './send-destination';

const doc = (text: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] });

beforeEach(() => {
	sessionStorage.clear();
});

describe('send destination (#1077)', () => {
	it('holds until a navigation or another chat', () => {
		const at = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		expect(stillAtDestination(at, { sessionId: 'a', editorOwner: 'a' })).toBe(true);
		expect(stillAtDestination(at, { sessionId: 'b', editorOwner: 'b' })).toBe(false);
		bumpInitGeneration();
		expect(stillAtDestination(at, { sessionId: 'a', editorOwner: 'a' })).toBe(false);
		// Reopened the same chat: not moved away, though the send is off.
		expect(movedAwayFrom(at, { sessionId: 'a', editorOwner: 'a' })).toBe(false);
		expect(movedAwayFrom(at, { sessionId: null, editorOwner: null })).toBe(true);
	});

	it('during a reload\'s restore the chat is the one the editor holds', () => {
		const at = captureDestination({ sessionId: null, editorOwner: 'a' });
		expect(at.chat).toBe('a');
		// The restore lands: the store adopts the same chat.
		expect(stillAtDestination(at, { sessionId: 'a', editorOwner: 'a' })).toBe(true);
		// New chat instead.
		bumpInitGeneration();
		expect(stillAtDestination(at, { sessionId: null, editorOwner: null })).toBe(false);
		expect(movedAwayFrom(at, { sessionId: null, editorOwner: null })).toBe(true);
	});

	it('a New chat that adopted the session its own send created has not moved (#1636)', () => {
		const at = captureDestination({ sessionId: null, editorOwner: null });
		expect(stillAtDestination(at, { sessionId: 'x', editorOwner: 'x' })).toBe(false);
		expect(movedAwayFrom(at, { sessionId: 'x', editorOwner: 'x' })).toBe(false);
		bumpInitGeneration();
		expect(movedAwayFrom(at, { sessionId: 'x', editorOwner: 'x' })).toBe(true);
	});

	it('keeps the newest text the chat held when the user left it', () => {
		noteLeftDraft('a', doc('left before Send'));
		const at = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		// Nothing left since Send: the words as sent.
		expect(draftToKeep(at, doc('sent'), false)).toBe(doc('sent'));
		noteLeftDraft('b', doc('another chat'));
		expect(draftToKeep(at, doc('sent'), false)).toBe(doc('sent'));
		// Left with edits made during the wait: those win.
		noteLeftDraft('a', doc('sent, corrected'));
		expect(draftToKeep(at, doc('sent'), false)).toBe(doc('sent, corrected'));
		// The composer had cleared for the send: a later message follows it.
		const kept = JSON.parse(draftToKeep(at, doc('sent'), true));
		expect(kept.content.map((p: { content: { text: string }[] }) => p.content[0].text)).toEqual(['sent', 'sent, corrected']);
		// Left empty: the unsent words are not dropped.
		noteLeftDraft('a', JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] }));
		expect(draftToKeep(at, doc('sent'), true)).toBe(doc('sent'));
	});

	it('keeps the draft in its chat, leaves New chat\'s alone, and says so', () => {
		const banner = vi.spyOn(notify, 'banner');
		writeDraft(null, doc('new chat words'));
		const at = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		keepUnsentDraft(at, doc('sent'), false);
		expect(readDraft('a')).toBe(doc('sent'));
		expect(readDraft(null)).toBe(doc('new chat words'));
		expect(banner).toHaveBeenCalledWith(SEND_MOVED_MESSAGE, expect.objectContaining({ severity: 'warning' }));
	});
});
