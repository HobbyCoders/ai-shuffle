/**
 * Pure classifiers for how the chat surface presents while the active session
 * has zero messages, and how ChatView lays it out — extracted from MainPanel /
 * ChatView so the logic can be unit-tested without the Svelte runtime.
 *
 * Background (see PR #558): clearing a session's conversation from the chat view
 * wipes its transcript IN PLACE — the session survives, the sdk_session_id is
 * nulled, and typing continues the SAME session id with fresh context. (The
 * sessions board's row action now DELETES a session outright rather than clearing
 * it in place, so this empty-but-bound state comes from the chat-view clear.) But an
 * open, titled session with zero messages previously rendered exactly like a
 * brand-new chat: header "New Chat" + the "Let's build" hero. Users read that as
 * "clearing deleted/replaced my session" — a top, twice-reported complaint.
 *
 * The fix keys presentation on whether the chat is BOUND to an existing session
 * (a non-null `sessionId`) rather than purely on message count:
 *   - new      → no session bound yet (a genuine New Chat)
 *   - cleared  → bound to an existing session, history settled, still empty
 *                (the just-cleared / emptied conversation)
 *   - loading  → bound, but history is still being fetched (boot restore or an
 *                openChat navigation). messages=[] is transient here, so we must
 *                NOT flash the cleared-empty hint for a session that will turn
 *                out to have messages.
 *   - active   → has messages
 *
 * Layout rule: a load keeps the layout the view already had (the centred hero
 * or the message column) until its outcome is known, so opening a chat moves
 * the composer at most once (createChatLayoutTracker). A loading chat in the
 * hero layout shows a bound "opening" hero, never the new-chat or cleared copy;
 * in the column it shows MessageArea's skeleton (classifyTranscriptRender). A
 * mirrored chat's first sync check counts as part of its load
 * (chatLayoutSurface).
 */

export type ChatSurface = 'new' | 'cleared' | 'loading' | 'active';

export interface ChatSurfaceInput {
	/** The session id the chat is bound to, or null for an unstarted New Chat. */
	sessionId: string | null | undefined;
	/** Number of messages currently in the active session store. */
	messageCount: number;
	/** Boot-restore window: adopting a persisted sessionId, history not yet fetched. */
	isRestoringSession: boolean;
	/** openChat navigation window: history fetch for the target session in flight. */
	isLoadingSession: boolean;
}

/**
 * Classify the current chat surface. `messageCount > 0` always wins (active);
 * otherwise an in-flight history load ('loading') defers the empty/new decision,
 * a bound-but-settled empty session is a 'cleared' conversation, and only an
 * unbound empty session is a genuine 'new' chat.
 */
export function classifyChatSurface(input: ChatSurfaceInput): ChatSurface {
	const { sessionId, messageCount, isRestoringSession, isLoadingSession } = input;
	if (messageCount > 0) return 'active';
	if (isRestoringSession || isLoadingSession) return 'loading';
	if (sessionId) return 'cleared';
	return 'new';
}

/**
 * How ChatView lays the surface out: the centred hero with the composer under
 * it, or the message column with the composer docked at the bottom.
 */
export type ChatLayout = 'hero' | 'column';

/** The layout a settled (not loading) surface has. */
export function settledChatLayout(surface: Exclude<ChatSurface, 'loading'>): ChatLayout {
	return surface === 'new' || surface === 'cleared' ? 'hero' : 'column';
}

/**
 * The layout for each surface as it changes, for one view.
 *
 * A 'loading' surface does not yet know whether the opened chat has a
 * transcript, so it keeps the layout the view already had instead of guessing.
 * Guessing 'column' (as before) moved the composer to the bottom for the load
 * and straight back to the centre when the chat turned out empty: a 1-3 frame
 * `centre → bottom → centre` flicker whenever an empty (cleared) chat was opened
 * from the new-session screen. Keeping the layout makes every open path move
 * the composer at most once, when the outcome is known:
 *
 *   new/cleared → loading (hero)   → active (column) | cleared (hero)
 *   active      → loading (column) → active (column) | cleared (hero)
 *
 * `initial` is the layout of a view that starts in 'loading' (boot restore or
 * a deep link): the column, whose skeleton is the usual outcome.
 *
 * Returns a function to call with the current surface on every change; it
 * remembers the last settled layout between calls.
 */
export function createChatLayoutTracker(initial: ChatLayout = 'column'): (surface: ChatSurface) => ChatLayout {
	let settled = initial;
	return (surface) => {
		if (surface === 'loading') return settled;
		settled = settledChatLayout(surface);
		return settled;
	};
}

/**
 * The surface ChatView's layout follows. A mirrored (remote) chat whose first
 * sync with its owning worker is still pending is part of the open: once the
 * owner catches up the transcript is reloaded, so presenting the mirror's copy
 * first (or its emptiness as "cleared") would show one conversation and then
 * swap it. Its layout is held like a load's until the sync check reveals it.
 * A genuinely new chat (nothing bound) never waits on a sync.
 */
export function chatLayoutSurface(surface: ChatSurface, opts: { mirrorSyncPending: boolean }): ChatSurface {
	if (opts.mirrorSyncPending && surface !== 'new') return 'loading';
	return surface;
}

/**
 * What the centred hero shows when ChatView is in the hero layout:
 *   - new      the new-session hero (agent + "Runs on …")
 *   - cleared  the chat's own title + "Conversation cleared"
 *   - loading  the chat's title + "Opening the conversation…" (a chat opened
 *              from the hero layout, its history on the way)
 *   - syncing  the chat's title + the mirror's "Checking sync with …" (a
 *              mirrored chat waiting for its first catch-up)
 */
export type ChatHeroKind = 'new' | 'cleared' | 'loading' | 'syncing';

export function chatHeroKind(input: { surface: ChatSurface; mirrorSyncPending: boolean }): ChatHeroKind {
	if (input.surface === 'new') return 'new';
	if (input.mirrorSyncPending) return 'syncing';
	if (input.surface === 'cleared') return 'cleared';
	// 'loading', or 'active' held in the hero by a load (chatLayoutSurface).
	return 'loading';
}

/** True only for a bound session, settled and empty — i.e. a cleared conversation. */
export function isClearedSession(input: ChatSurfaceInput): boolean {
	return classifyChatSurface(input) === 'cleared';
}

/** The title an unstarted or not-yet-named chat carries in the session store. */
export const NEW_CHAT_TITLE = 'New Chat';

export interface ChatHeaderTitleInput {
	/** The session id the chat is bound to, or null for an unstarted New Chat. */
	sessionId: string | null | undefined;
	/** The session's title ('New Chat' for an unstarted/unnamed chat). */
	title: string | null | undefined;
	/** Number of messages currently in the active session store. */
	messageCount: number;
	/** The bound session's history is still loading (boot restore or deep link),
	 *  or a mirrored chat's first sync with its owner is still pending. */
	loading?: boolean;
	/** The chat's title in the session list. Names the chat while the store
	 *  still holds the 'New Chat' placeholder (a mirror whose /live has not
	 *  named it yet, a restore before the row arrived). */
	listTitle?: string | null;
}

/**
 * Resolve the chat header title. A BOUND session (sessionId set) always shows its
 * own title — including when it currently has zero messages (cleared / mid-load),
 * which previously fell through to a misleading "New Chat". Falls back to
 * 'Untitled' for a bound session with no title, and to 'New Chat' only for a
 * genuinely unbound, empty chat.
 *
 * While a bound chat loads, a blank title and the 'New Chat' placeholder both
 * mean "not known yet" (the switcher and boot restore fall back to the
 * placeholder until the row or /live names the chat), so it reads 'Loading…':
 * an existing chat is never called "New Chat" while it opens. The session
 * list's title, when it has one, names the chat before the store does. The
 * header and ChatView's bound hero both use this rule with the same inputs,
 * so they never disagree (#812).
 */
export function chatHeaderTitle(input: ChatHeaderTitleInput): string {
	const { sessionId, title, messageCount, loading = false, listTitle } = input;
	if (sessionId || messageCount > 0) {
		const own = title?.trim() || '';
		const listed = listTitle?.trim() || '';
		const known = (!own || own === NEW_CHAT_TITLE) && sessionId && listed && listed !== NEW_CHAT_TITLE ? listed : own;
		if (loading && sessionId && (!known || known === NEW_CHAT_TITLE)) return 'Loading…';
		// A deep link binds before its title is known: say it is loading rather
		// than call the chat Untitled for the moment before /live answers.
		return known || (loading ? 'Loading…' : 'Untitled');
	}
	return NEW_CHAT_TITLE;
}

/**
 * What MessageArea should draw for the current surface.
 *
 * When a load starts in the message column (opened from another chat, or a
 * boot restore / deep link), ChatView keeps that column, which contains
 * MessageArea — whose empty
 * branch keyed purely on `messages.length === 0` and therefore drew the full
 * "Start a Conversation" hero on EVERY switch, indistinguishable from a
 * brand-new empty chat, before the incoming transcript popped in.
 *
 * The loading window gets a transcript-shaped skeleton instead; a genuinely
 * empty session (new or cleared, both settled) still gets the real hero.
 */
export type TranscriptRender = 'skeleton' | 'empty' | 'transcript';

export function classifyTranscriptRender(input: ChatSurfaceInput): TranscriptRender {
	const surface = classifyChatSurface(input);
	if (surface === 'active') return 'transcript';
	if (surface === 'loading') return 'skeleton';
	return 'empty';
}
