/**
 * Tests for how the chat surface presents when the active session has zero
 * messages.
 *
 * Regression guard for the "cleared session renders as a brand-new chat" bug
 * (see #558): the sessions-board broom clears a session's transcript IN PLACE —
 * the row survives, the sdk_session_id is nulled, and typing continues the SAME
 * session id. But an open, TITLED session with zero messages previously showed
 * header "New Chat" and the "Let's build" hero, so users believed clearing had
 * deleted/replaced their session (a top, twice-reported complaint).
 *
 * The fix keys presentation on whether the chat is BOUND to an existing session
 * (a non-null sessionId), and defers the empty/new decision while history is
 * still loading so the cleared hint can't flash for a session that turns out to
 * have messages.
 */

import { describe, it, expect } from 'vitest';
import {
	classifyChatSurface,
	isEmptyChatSurface,
	isClearedSession,
	chatHeaderTitle,
	classifyTranscriptRender,
	type ChatSurfaceInput
} from './chat-empty-state';

/** A genuinely new, unbound compose screen. */
function newChat(overrides: Partial<ChatSurfaceInput> = {}): ChatSurfaceInput {
	return {
		sessionId: null,
		messageCount: 0,
		isRestoringSession: false,
		isLoadingSession: false,
		...overrides
	};
}

/** A bound existing session, settled and empty — i.e. just cleared. */
function clearedSession(overrides: Partial<ChatSurfaceInput> = {}): ChatSurfaceInput {
	return { ...newChat(), sessionId: 'a419a113', ...overrides };
}

describe('classifyChatSurface', () => {
	it('classifies an unbound, empty chat as "new"', () => {
		expect(classifyChatSurface(newChat())).toBe('new');
	});

	it('classifies a bound, settled, empty session as "cleared"', () => {
		expect(classifyChatSurface(clearedSession())).toBe('cleared');
	});

	it('classifies a bound session mid boot-restore as "loading" (not cleared)', () => {
		// The restore window: sessionId is set, messages is still [], history not
		// yet fetched. Must NOT flash the cleared hint for a session that will
		// turn out to have messages.
		expect(classifyChatSurface(clearedSession({ isRestoringSession: true }))).toBe('loading');
	});

	it('classifies a bound session mid openChat load as "loading" (not cleared)', () => {
		expect(classifyChatSurface(clearedSession({ isLoadingSession: true }))).toBe('loading');
	});

	it('classifies any session with messages as "active" (message count wins)', () => {
		expect(classifyChatSurface(clearedSession({ messageCount: 3 }))).toBe('active');
		expect(classifyChatSurface(newChat({ messageCount: 1 }))).toBe('active');
		// Even mid-load, present messages mean active.
		expect(
			classifyChatSurface(clearedSession({ messageCount: 2, isLoadingSession: true }))
		).toBe('active');
	});
});

describe('isEmptyChatSurface', () => {
	it('is true for a new chat and a cleared session', () => {
		expect(isEmptyChatSurface(newChat())).toBe(true);
		expect(isEmptyChatSurface(clearedSession())).toBe(true);
	});

	it('is false while history is loading (renders the message column instead)', () => {
		expect(isEmptyChatSurface(clearedSession({ isRestoringSession: true }))).toBe(false);
		expect(isEmptyChatSurface(clearedSession({ isLoadingSession: true }))).toBe(false);
	});

	it('is false once there are messages', () => {
		expect(isEmptyChatSurface(clearedSession({ messageCount: 1 }))).toBe(false);
	});
});

describe('isClearedSession', () => {
	it('is true ONLY for a bound, settled, empty session', () => {
		expect(isClearedSession(clearedSession())).toBe(true);
	});

	it('is false for an unbound new chat', () => {
		expect(isClearedSession(newChat())).toBe(false);
	});

	it('is false while the bound session is still loading (no premature hint)', () => {
		expect(isClearedSession(clearedSession({ isLoadingSession: true }))).toBe(false);
		expect(isClearedSession(clearedSession({ isRestoringSession: true }))).toBe(false);
	});

	it('is false once the bound session has messages', () => {
		expect(isClearedSession(clearedSession({ messageCount: 4 }))).toBe(false);
	});
});

describe('chatHeaderTitle', () => {
	it('shows "New Chat" for an unbound, empty chat', () => {
		expect(chatHeaderTitle({ sessionId: null, title: 'New Chat', messageCount: 0 })).toBe(
			'New Chat'
		);
	});

	it('shows the session title for a BOUND empty session (the core fix)', () => {
		// The cleared session a419a113 titled "Reply with exactly the word OK…":
		// zero messages must NOT collapse to "New Chat".
		expect(
			chatHeaderTitle({
				sessionId: 'a419a113',
				title: 'Reply with exactly the word OK and nothing else.',
				messageCount: 0
			})
		).toBe('Reply with exactly the word OK and nothing else.');
	});

	it('shows the session title for a bound session while it loads', () => {
		// messageCount 0 during the openChat/restore window still resolves to the
		// real title (header point 1), never "New Chat".
		expect(
			chatHeaderTitle({ sessionId: 'sess-1', title: 'Refactor the poller', messageCount: 0 })
		).toBe('Refactor the poller');
	});

	it('falls back to "Untitled" for a bound session with no/blank title', () => {
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: '', messageCount: 0 })).toBe('Untitled');
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: '   ', messageCount: 0 })).toBe(
			'Untitled'
		);
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: null, messageCount: 0 })).toBe('Untitled');
	});

	it('shows the title once an unbound chat has messages (first-send window)', () => {
		expect(chatHeaderTitle({ sessionId: null, title: 'My task', messageCount: 2 })).toBe('My task');
	});
});


/**
 * MessageArea renders the message column for the 'loading' surface, so its own
 * empty branch has to know about the load window too — otherwise every session
 * switch flashed the full "Start a Conversation" hero between the old
 * transcript and the new one (PERFORMANCE_REVIEW §3.1 S3).
 */
describe('classifyTranscriptRender', () => {
	it('draws a skeleton while a bound session hydrates', () => {
		expect(
			classifyTranscriptRender({
				sessionId: 'sess-1',
				messageCount: 0,
				isRestoringSession: false,
				isLoadingSession: true
			})
		).toBe('skeleton');
		expect(
			classifyTranscriptRender({
				sessionId: 'sess-1',
				messageCount: 0,
				isRestoringSession: true,
				isLoadingSession: false
			})
		).toBe('skeleton');
	});

	it('still shows the real hero for a genuinely empty session', () => {
		// Brand-new, unbound chat.
		expect(
			classifyTranscriptRender({
				sessionId: null,
				messageCount: 0,
				isRestoringSession: false,
				isLoadingSession: false
			})
		).toBe('empty');
		// Bound but settled and empty — a cleared conversation.
		expect(
			classifyTranscriptRender({
				sessionId: 'sess-1',
				messageCount: 0,
				isRestoringSession: false,
				isLoadingSession: false
			})
		).toBe('empty');
	});

	it('draws the transcript as soon as there are messages, load or no load', () => {
		expect(
			classifyTranscriptRender({
				sessionId: 'sess-1',
				messageCount: 12,
				isRestoringSession: false,
				isLoadingSession: true
			})
		).toBe('transcript');
	});
});
