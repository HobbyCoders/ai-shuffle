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
	chatHeroKind,
	chatLayoutSurface,
	isClearedSession,
	chatHeaderTitle,
	classifyTranscriptRender,
	createChatLayoutTracker,
	type ChatSurface,
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
	it('(#913) keeps a load released without any history "loading", never "cleared"', () => {
		// Running out of time is not evidence of emptiness: only a completed
		// response or a clear may present a bound chat as cleared.
		expect(classifyChatSurface(clearedSession({ historyStalled: true }))).toBe('loading');
		expect(classifyTranscriptRender(clearedSession({ historyStalled: true }))).toBe('skeleton');
		// A send made meanwhile is the chat's own row: it is shown.
		expect(classifyChatSurface(clearedSession({ historyStalled: true, messageCount: 1 }))).toBe('active');
	});

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

	it('says "Loading…" for a bound session whose title has not arrived yet', () => {
		// A deep link binds the chat before /live answers: its title is still
		// blank, and "Untitled" would misstate a chat that has a name.
		expect(
			chatHeaderTitle({ sessionId: 'sess-1', title: '', messageCount: 0, loading: true })
		).toBe('Loading…');
		expect(
			chatHeaderTitle({ sessionId: 'sess-1', title: 'Named', messageCount: 0, loading: true })
		).toBe('Named');
	});

	it('never calls an existing chat "New Chat" while it loads (the placeholder is not a name)', () => {
		// The switcher and boot restore fall back to the 'New Chat' placeholder
		// until the sidebar row or /live names the chat.
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: 'New Chat', messageCount: 0, loading: true })).toBe('Loading…');
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: '  New Chat ', messageCount: 0, loading: true })).toBe('Loading…');
		// Settled, it is the chat's stored title like any other.
		expect(chatHeaderTitle({ sessionId: 'sess-1', title: 'New Chat', messageCount: 0 })).toBe('New Chat');
		// An unbound compose is the new chat.
		expect(chatHeaderTitle({ sessionId: null, title: 'New Chat', messageCount: 0, loading: true })).toBe('New Chat');
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

	it('names a placeholder-titled chat from the session list (#812)', () => {
		// A mirrored chat in its first sync: the store still says 'New Chat'.
		const syncing = { sessionId: 'sess-1', title: 'New Chat', messageCount: 0, loading: true };
		expect(chatHeaderTitle({ ...syncing, listTitle: 'Fix the poller' })).toBe('Fix the poller');
		expect(chatHeaderTitle({ ...syncing, listTitle: null })).toBe('Loading…');
		expect(chatHeaderTitle({ ...syncing, listTitle: 'New Chat' })).toBe('Loading…');
		// The store's own title wins once it has one.
		expect(chatHeaderTitle({ ...syncing, title: 'Renamed', listTitle: 'Fix the poller' })).toBe('Renamed');
		// Settled: the list's title still beats the placeholder.
		expect(chatHeaderTitle({ ...syncing, loading: false, listTitle: 'Fix the poller' })).toBe('Fix the poller');
		// An unbound compose never borrows a list title.
		expect(chatHeaderTitle({ sessionId: null, title: 'New Chat', messageCount: 0, listTitle: 'Other' })).toBe('New Chat');
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

/**
 * Opening a chat whose history is not loaded yet used to dock the composer for
 * the load and, when the chat turned out empty (cleared), snap it back to the
 * centre 1-3 frames later (flight recorder: composer centre → bottom → centre).
 * A load now keeps the layout the view already had, so the composer moves at
 * most once on every path that opens a chat.
 */
describe('createChatLayoutTracker', () => {
	const run = (surfaces: ChatSurface[], initial?: 'hero' | 'column') => {
		const layout = createChatLayoutTracker(initial);
		return surfaces.map((surface) => layout(surface));
	};
	/** How many times the layout changes along a sequence. */
	const moves = (layouts: string[]) => layouts.filter((l, i) => i > 0 && l !== layouts[i - 1]).length;

	it('keeps the hero while an empty chat opened from the new-session screen loads', () => {
		const layouts = run(['new', 'loading', 'cleared']);
		expect(layouts).toEqual(['hero', 'hero', 'hero']);
		expect(moves(layouts)).toBe(0);
	});

	it('moves to the column once when the opened chat has a transcript', () => {
		const layouts = run(['new', 'loading', 'active']);
		expect(layouts).toEqual(['hero', 'hero', 'column']);
		expect(moves(layouts)).toBe(1);
	});

	it('keeps the column (and its skeleton) while a chat opened from another chat loads', () => {
		expect(run(['active', 'loading', 'active'])).toEqual(['column', 'column', 'column']);
		expect(run(['active', 'loading', 'cleared'])).toEqual(['column', 'column', 'hero']);
	});

	it('loads from a cleared chat in the hero layout too', () => {
		expect(run(['cleared', 'loading', 'active'])).toEqual(['hero', 'hero', 'column']);
		expect(run(['cleared', 'loading', 'cleared'])).toEqual(['hero', 'hero', 'hero']);
	});

	it('starts a boot restore or deep link in the column, the usual outcome', () => {
		expect(run(['loading', 'active'])).toEqual(['column', 'column']);
		expect(run(['loading', 'cleared'])).toEqual(['column', 'hero']);
		expect(run(['loading'], 'hero')).toEqual(['hero']);
	});

	it('never goes back to a layout it left within one open, whatever the order of chats', () => {
		const surfaces: ChatSurface[] = ['new', 'cleared', 'active'];
		for (const from of surfaces) {
			for (const to of surfaces) {
				const layouts = run([from, 'loading', 'loading', to]);
				expect(moves(layouts), `${from} → loading → ${to}: ${layouts.join(' → ')}`).toBeLessThanOrEqual(1);
			}
		}
	});

	it('remembers the last settled layout across several loads', () => {
		const layout = createChatLayoutTracker();
		expect(layout('new')).toBe('hero');
		expect(layout('loading')).toBe('hero');
		expect(layout('active')).toBe('column');
		expect(layout('loading')).toBe('column');
		expect(layout('cleared')).toBe('hero');
		expect(layout('loading')).toBe('hero');
	});
});

/**
 * A mirrored chat (owned by another worker) is checked against its owner before
 * it is shown, and its first catch-up reloads the transcript. That check is
 * part of the open: it runs whatever the layout, and the layout waits for it
 * like it waits for a load, so a remote chat opened from the hero never shows
 * its mirror's transcript and then swaps it for a "Checking sync…" pane.
 */
describe('chatLayoutSurface (a mirrored chat\'s first sync)', () => {
	it('holds the layout like a load while the first sync is pending', () => {
		expect(chatLayoutSurface('active', { mirrorSyncPending: true })).toBe('loading');
		expect(chatLayoutSurface('cleared', { mirrorSyncPending: true })).toBe('loading');
		expect(chatLayoutSurface('loading', { mirrorSyncPending: true })).toBe('loading');
	});

	it('is the surface itself once synced, and for a chat nothing is bound to', () => {
		for (const surface of ['new', 'cleared', 'loading', 'active'] as ChatSurface[]) {
			expect(chatLayoutSurface(surface, { mirrorSyncPending: false })).toBe(surface);
		}
		expect(chatLayoutSurface('new', { mirrorSyncPending: true })).toBe('new');
	});

	/** One open of a remote chat: [surface, sync pending] per step. */
	const open = (steps: Array<[ChatSurface, boolean]>, initial?: 'hero' | 'column') => {
		const layout = createChatLayoutTracker(initial);
		return steps.map(([surface, pending]) => ({
			layout: layout(chatLayoutSurface(surface, { mirrorSyncPending: pending })),
			hero: chatHeroKind({ surface, mirrorSyncPending: pending })
		}));
	};
	const moves = (layouts: string[]) => layouts.filter((l, i) => i > 0 && l !== layouts[i - 1]).length;

	it('opened from the hero: the history lands while the sync is still checking, then the reload', () => {
		// new → open (loading, sync starts at once) → history arrives (active,
		// still checking) → caught up: reload (loading) → transcript.
		const steps = open([
			['new', false],
			['loading', true],
			['active', true],
			['loading', false],
			['active', false]
		]);
		expect(steps.map((s) => s.layout)).toEqual(['hero', 'hero', 'hero', 'hero', 'column']);
		expect(moves(steps.map((s) => s.layout))).toBe(1);
		// The hero says what it waits for, and never the new-chat or cleared copy.
		expect(steps.map((s) => s.hero)).toEqual(['new', 'syncing', 'syncing', 'loading', 'loading']);
	});

	it('opened from the hero, empty on the mirror: never "cleared" before the owner says so', () => {
		const steps = open([
			['new', false],
			['loading', true],
			['cleared', true],
			['loading', false],
			['cleared', false]
		]);
		expect(steps.map((s) => s.layout)).toEqual(['hero', 'hero', 'hero', 'hero', 'hero']);
		expect(steps.map((s) => s.hero)).toEqual(['new', 'syncing', 'syncing', 'loading', 'cleared']);
	});

	it('opened from another chat: the column stays (MirrorSyncGate shows the check there)', () => {
		const layouts = open([
			['active', false],
			['loading', true],
			['active', true],
			['loading', false],
			['active', false]
		]).map((s) => s.layout);
		expect(layouts).toEqual(['column', 'column', 'column', 'column', 'column']);
	});

	it('never moves the composer more than once on any order of outcomes', () => {
		const outcomes: ChatSurface[] = ['cleared', 'active'];
		for (const from of ['new', 'cleared', 'active'] as ChatSurface[]) {
			for (const mirror of outcomes) {
				for (const final of outcomes) {
					const layouts = open([
						[from, false],
						['loading', true],
						[mirror, true],
						['loading', false],
						[final, false]
					]).map((s) => s.layout);
					expect(moves(layouts), `${from} → ${mirror} (syncing) → ${final}: ${layouts.join(' → ')}`).toBeLessThanOrEqual(1);
				}
			}
		}
	});
});

describe('chatHeroKind', () => {
	it('is the new-session hero only for an unbound chat', () => {
		expect(chatHeroKind({ surface: 'new', mirrorSyncPending: false })).toBe('new');
		expect(chatHeroKind({ surface: 'cleared', mirrorSyncPending: false })).toBe('cleared');
		expect(chatHeroKind({ surface: 'loading', mirrorSyncPending: false })).toBe('loading');
		// Held in the hero by a load: still the loading hero, never the new one.
		expect(chatHeroKind({ surface: 'active', mirrorSyncPending: false })).toBe('loading');
		expect(chatHeroKind({ surface: 'cleared', mirrorSyncPending: true })).toBe('syncing');
	});
});
