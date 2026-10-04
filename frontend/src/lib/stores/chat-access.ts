/**
 * chat-access — what the private-access panel for the active chat is showing.
 *
 * ChatAccess owns the polling, the request cards and the dialog. The chat
 * header only needs two things from it: how many approvals are waiting (that
 * outranks every git state on the header button) and a way to open the
 * dialog from the git panel's "Private access" row. ChatAccess publishes both
 * here whenever its snapshot changes and clears the entry when it unmounts.
 */
import { writable } from 'svelte/store';

/** One thing in the panel that waits for the owner, as the chat shows it. */
export interface PendingAccessItem {
	id: string;
	/** `machine` / `folder` / `service` for a request; `action` for a one-shot approval. */
	kind: 'machine' | 'folder' | 'service' | 'action';
	/** The agent's stated purpose, or what the one-shot action does. */
	detail: string;
}

export interface ChatAccessSummary {
	sessionId: string;
	/** Requests + one-shot actions waiting for the user. */
	pending: number;
	/** The waiting items themselves, so the chat can show them (#1807). */
	items?: PendingAccessItem[];
	/** Grants currently active in this chat. */
	active: number;
	/** Saved connections listed in the panel. */
	saved: number;
	/** The last refresh failed; the panel shows the error. */
	error: boolean;
	/** Open the private-access dialog. No-op when there is nothing to show. */
	open: () => void;
}

/** Summary for the mounted ChatAccess instance, or null when none is mounted. */
export const chatAccessSummary = writable<ChatAccessSummary | null>(null);

/**
 * Private-access items waiting for the owner, per session (#1807). The open
 * chat's panel writes its own count after every snapshot; the worker's
 * `chat_access_changed` event carries the count for every other session. The
 * sidebar reads it to put a waiting session under "Needs you". Kept apart from
 * the permission/question attention map, which the server replaces wholesale.
 */
export const privateAccessPending = writable<Map<string, number>>(new Map());

export function setPrivateAccessPending(sessionId: string, count: number): void {
	privateAccessPending.update((map) => {
		const current = map.get(sessionId) ?? 0;
		const next = Math.max(0, Math.floor(count)) || 0;
		if (current === next) return map;
		const copy = new Map(map);
		if (next > 0) copy.set(sessionId, next);
		else copy.delete(sessionId);
		return copy;
	});
}
