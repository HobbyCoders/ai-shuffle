/**
 * Text another surface hands to a chat's composer (the terminal's "Send
 * selection to chat"). The composer that owns that chat appends it to the
 * draft and never sends it. A request for a chat whose composer is not
 * mounted waits until it is.
 */
import { writable } from 'svelte/store';

export interface ComposerInsert {
	sessionId: string;
	text: string;
	seq: number;
}

let seq = 0;

export const composerInsert = writable<ComposerInsert | null>(null);

export function requestComposerInsert(sessionId: string, text: string): void {
	if (!sessionId || !text) return;
	seq += 1;
	composerInsert.set({ sessionId, text, seq });
}

/** The composer took the request: clear it unless a newer one replaced it. */
export function completeComposerInsert(done: ComposerInsert): void {
	composerInsert.update((current) => (current?.seq === done.seq ? null : current));
}
