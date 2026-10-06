/**
 * A chat the app tried to open and the server would not serve: no such
 * session (404, e.g. a deep link with a short or mistyped id) or not readable
 * by this viewer (403).
 *
 * The store leaves such a chat for an empty New Chat (#1908). Without this
 * record that New Chat was all the user saw, with no word on why the chat they
 * asked for did not open. The chat surface shows a plain notice while the
 * composer is still unbound; opening any chat or starting a new one clears it.
 */
import { writable } from 'svelte/store';
import { pendingDeepLink } from './pending-deep-link';

export interface UnavailableChat {
	sessionId: string;
	/** HTTP status the server answered with (404 not found, 403 not allowed). */
	status: number;
}

export const unavailableChat = writable<UnavailableChat | null>(null);

/** Record that `sessionId` could not be opened. */
export function markChatUnavailable(sessionId: string, status = 404): void {
	unavailableChat.set({ sessionId, status });
	// A deep link to it is answered: stop presenting the surface as loading.
	pendingDeepLink.update((id) => (id === sessionId ? null : id));
}

/** Forget the notice: the user moved on (opened a chat or started a new one). */
export function clearUnavailableChat(): void {
	unavailableChat.set(null);
}

/** Plain-language copy for the notice. */
export function unavailableChatCopy(chat: UnavailableChat): { title: string; detail: string } {
	if (chat.status === 403) {
		return {
			title: 'You can’t open this chat',
			detail: 'This chat belongs to another account or device you don’t have access to.'
		};
	}
	return {
		title: 'Chat not found',
		detail: 'No chat matches this link. It may have been deleted, or the link may be incomplete.'
	};
}
