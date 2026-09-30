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

export interface ChatAccessSummary {
	sessionId: string;
	/** Requests + one-shot actions waiting for the user. */
	pending: number;
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
