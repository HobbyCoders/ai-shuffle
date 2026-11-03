/**
 * Which chat this tab last saw settled with no history.
 *
 * Whether a chat is empty is only known once its history arrives, but the
 * composer has to paint somewhere before that: docked under a transcript or
 * centred under the hero. After a reload the tab's own last look at the chat is
 * the best answer available at first paint, so an empty chat opens centred and
 * stays there instead of starting docked and moving (#1049). Only the first
 * layout depends on this: the loaded history still decides, so a stale answer
 * costs the one move any load may make.
 *
 * Per tab, like the tab's active session and its drafts. One chat is enough: a
 * tab only ever reloads into the chat it last showed, and showing a chat settles
 * it here again. Storage failures degrade to the docked first layout.
 */

export const EMPTY_CHAT_KEY = '__aihub_empty_chat';

/** Record what a settled chat turned out to be: this id is empty, null is not. */
export function rememberEmptyChat(sessionId: string | null): void {
	try {
		if (typeof sessionStorage === 'undefined') return;
		if (sessionId) sessionStorage.setItem(EMPTY_CHAT_KEY, sessionId);
		else sessionStorage.removeItem(EMPTY_CHAT_KEY);
	} catch {
		// Storage unavailable: the first layout falls back to the docked composer.
	}
}

/** Whether this tab last saw this chat settled with no history. */
export function wasLastSeenEmpty(sessionId: string | null | undefined): boolean {
	if (!sessionId) return false;
	try {
		if (typeof sessionStorage === 'undefined') return false;
		return sessionStorage.getItem(EMPTY_CHAT_KEY) === sessionId;
	} catch {
		return false;
	}
}
