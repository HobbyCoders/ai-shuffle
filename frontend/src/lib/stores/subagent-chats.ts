import { writable } from 'svelte/store';

/**
 * Chats this tab knows are a subagent's own (a Task or Swarm child; the
 * server's `is_worker` flag). Such a chat is for reading the transcript and
 * messaging the subagent: it shows no Git controls and no private access UI.
 * The subagent's private access requests and approvals show in its main chat
 * instead. Fed by the row a chat is opened with, the session's /live metadata
 * on switch-in (covers reloads and deep links), and the Task and Swarm cards'
 * "open transcript" links.
 */
const store = writable<ReadonlySet<string>>(new Set());

export const subagentChats = {
	subscribe: store.subscribe,
	/** Record whether `sessionId` is a subagent's chat. No-op when nothing changes. */
	mark(sessionId: string, subagent: boolean) {
		store.update((ids) => {
			if (ids.has(sessionId) === subagent) return ids;
			const next = new Set(ids);
			if (subagent) next.add(sessionId);
			else next.delete(sessionId);
			return next;
		});
	},
};
