/**
 * A `/?session=<id>` deep link that boot has read but not opened yet.
 *
 * The app opens a deep-linked session only after init and the first data
 * loads, because the session row decides its lane and worktree. Until then the
 * active session is unbound, and the chat surface read that as a New Chat:
 * the "Let's build" hero flashed before the linked chat replaced it. While a
 * link is pending, the chat surface shows its loading state instead.
 */
import { writable } from 'svelte/store';

/** The session id a deep link is about to open, or null. */
export const pendingDeepLink = writable<string | null>(null);

/** Read the link from a URL search string, without consuming it. */
export function deepLinkSessionId(search: string): string | null {
	const id = new URLSearchParams(search).get('session');
	return id ? id : null;
}

/**
 * Whether the chat surface should present as loading because of a pending
 * deep link: one is pending and the active session is not that session yet.
 */
export function deepLinkLoading(pending: string | null, activeSessionId: string | null | undefined): boolean {
	return pending !== null && activeSessionId !== pending;
}
