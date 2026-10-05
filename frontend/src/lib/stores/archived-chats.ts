import { writable } from 'svelte/store';

/**
 * Chats this tab knows are archived (#1822). An archived chat is a read-only
 * transcript until the owner restores it, so the chat view shows it without a
 * composer. Fed by the row a chat is opened with, the session's /live
 * metadata on switch-in (covers reloads and deep links), and this tab's own
 * archive / restore actions.
 */
const store = writable<ReadonlySet<string>>(new Set());

export const archivedChats = {
	subscribe: store.subscribe,
	/** Record whether `sessionId` is archived. No-op when nothing changes. */
	mark(sessionId: string, archived: boolean) {
		store.update((ids) => {
			if (ids.has(sessionId) === archived) return ids;
			const next = new Set(ids);
			if (archived) next.add(sessionId);
			else next.delete(sessionId);
			return next;
		});
	},
};
