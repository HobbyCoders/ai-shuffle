/**
 * Pure classifiers for how the chat surface should present when the active
 * session has zero messages — extracted from MainPanel / ChatView so the logic
 * can be unit-tested without the Svelte runtime.
 *
 * Background (see PR #558): the sessions-board "Clear session" broom wipes a
 * session's transcript IN PLACE — the row survives, the sdk_session_id is
 * nulled, and typing continues the SAME session id with fresh context. But an
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
 * Whether ChatView should render its centered empty surface (the New-Chat hero
 * or the cleared-conversation hero) rather than the active message column.
 * True for 'new' and 'cleared'; 'loading' and 'active' render the message column
 * (matching the pre-existing restore-window behaviour that avoids sends landing
 * in an invisible session).
 */
export function isEmptyChatSurface(input: ChatSurfaceInput): boolean {
	const surface = classifyChatSurface(input);
	return surface === 'new' || surface === 'cleared';
}

/** True only for a bound session, settled and empty — i.e. a cleared conversation. */
export function isClearedSession(input: ChatSurfaceInput): boolean {
	return classifyChatSurface(input) === 'cleared';
}

export interface ChatHeaderTitleInput {
	/** The session id the chat is bound to, or null for an unstarted New Chat. */
	sessionId: string | null | undefined;
	/** The session's title ('New Chat' for an unstarted/unnamed chat). */
	title: string | null | undefined;
	/** Number of messages currently in the active session store. */
	messageCount: number;
}

/**
 * Resolve the chat header title. A BOUND session (sessionId set) always shows its
 * own title — including when it currently has zero messages (cleared / mid-load),
 * which previously fell through to a misleading "New Chat". Falls back to
 * 'Untitled' for a bound session with no title, and to 'New Chat' only for a
 * genuinely unbound, empty chat.
 */
export function chatHeaderTitle(input: ChatHeaderTitleInput): string {
	const { sessionId, title, messageCount } = input;
	if (sessionId || messageCount > 0) {
		return title?.trim() || 'Untitled';
	}
	return 'New Chat';
}
