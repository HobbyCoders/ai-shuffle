import { beforeEach, describe, expect, it, vi } from 'vitest';
import { bumpInitGeneration } from '$lib/stores/session/session-init';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
import { notify } from '$lib/stores/notifications';
import { hasComposerDraft as hasText } from '$lib/stores/session/composer-suggestion';
import {
	captureDestination, draftToKeep, keepRefusedSend, keepUnsentDraft, movedAwayFrom, noteLeftDraft, SEND_MOVED_MESSAGE, stillAtDestination
} from './send-destination';

const doc = (text: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] });
const EMPTY = JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] });
const texts = (draft: string | null) =>
	JSON.parse(draft ?? '{"content":[]}').content.map((p: { content?: { text: string }[] }) => p.content?.[0]?.text ?? '');

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

	// Review S4: two called-off sends for one chat share the text typed after them.
	it('keeps every called-off message for a chat, in send order, whichever recovers first', () => {
		for (const order of [[0, 1], [1, 0]]) {
			sessionStorage.clear();
			const sent = [captureDestination({ sessionId: 'a', editorOwner: 'a' }), captureDestination({ sessionId: 'a', editorOwner: 'a' })];
			noteLeftDraft('a', doc('third draft'));
			for (const i of order) keepUnsentDraft(sent[i], doc(i === 0 ? 'first' : 'second'), true);
			expect(texts(readDraft('a'))).toEqual(['first', 'second', 'third draft']);
		}
	});

	// Review S7: a revisit between two recoveries used to forget the first one's place.
	it('keeps send order when the chat is revisited between two recoveries', () => {
		const sent = [captureDestination({ sessionId: 'a', editorOwner: 'a' }), captureDestination({ sessionId: 'a', editorOwner: 'a' })];
		noteLeftDraft('a', doc('third'));
		keepUnsentDraft(sent[0], doc('first'), true);
		expect(texts(readDraft('a'))).toEqual(['first', 'third']);
		// Reopen chat A (its draft shows) and leave it again.
		noteLeftDraft('b', EMPTY);
		noteLeftDraft('a', readDraft('a')!);
		keepUnsentDraft(sent[1], doc('second'), true);
		expect(texts(readDraft('a'))).toEqual(['first', 'second', 'third']);
	});

	it('a later called-off send does not bring back messages already recovered', () => {
		const first = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		noteLeftDraft('a', EMPTY);
		keepUnsentDraft(first, doc('first'), true);
		// Back in chat A, the recovered text was sent; a new message is called off.
		noteLeftDraft('b', EMPTY);
		const next = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		noteLeftDraft('a', EMPTY);
		keepUnsentDraft(next, doc('next'), true);
		expect(texts(readDraft('a'))).toEqual(['next']);
	});

	// Review S5: before the composer cleared, an emptied editor is what the user wants.
	it('an editor emptied during the wait wins over the text as it was at Send', () => {
		const at = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		noteLeftDraft('a', EMPTY);
		expect(hasText(draftToKeep(at, doc('sent'), false))).toBe(false);
		keepUnsentDraft(at, doc('sent'), false);
		expect(readDraft('a')).toBeNull();
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
	// #1444: a send refused after its composer went reads the chat's draft at
	// that moment, never what a composer held when it left the chat.
	it('puts a send refused after its composer went in front of the chat\'s draft as it is now', () => {
		const at = captureDestination({ sessionId: 'a', editorOwner: 'a' });
		noteLeftDraft('a', EMPTY);
		// A later composer on chat A saved newer text since.
		writeDraft('a', doc('newer'));
		keepRefusedSend(at, 'a', doc('sent'));
		expect(texts(readDraft('a'))).toEqual(['sent', 'newer']);
		expect(readDraft(null)).toBeNull();
	});

	it('keeps both a refused send and a called-off one for a chat, in send order, whichever lands first', () => {
		for (const gone of [0, 1]) {
			sessionStorage.clear();
			const sent = [captureDestination({ sessionId: 'a', editorOwner: 'a' }), captureDestination({ sessionId: 'a', editorOwner: 'a' })];
			noteLeftDraft('a', doc('third draft'));
			writeDraft('a', doc('third draft'));
			const words = (i: number) => doc(i === 0 ? 'first' : 'second');
			if (gone === 0) {
				keepRefusedSend(sent[0], 'a', words(0));
				keepUnsentDraft(sent[1], words(1), true);
			} else {
				keepUnsentDraft(sent[0], words(0), true);
				keepRefusedSend(sent[1], 'a', words(1));
			}
			expect(texts(readDraft('a'))).toEqual(['first', 'second', 'third draft']);
		}
	});
});
