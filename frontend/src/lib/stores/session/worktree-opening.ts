import { writable } from 'svelte/store';
import { api } from '$lib/api/client';

// The draft prefix makes account changes clear the note with other tab drafts.
const key = '__aihub_draft_worktree-opening';
export interface Opening {
	id: string; project: string; shared: string | null; generation: number;
	prompt: string; paths?: string[]; dispatched?: boolean;
}
export function pendingOpening(): Opening | null {
	try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; }
}
// Hold the surface from the first frame, before boot has established which
// account and project may see the words. Only a validated scope enables lookup.
const initial = pendingOpening();
export const recoveringOpening = writable<{ note: Opening; ready: boolean; adopt?: (id: string) => void } | null>(
	initial?.dispatched ? { note: initial, ready: false } : null);
export function keepOpening(note: Opening) {
	try { sessionStorage.setItem(key, JSON.stringify(note)); } catch { /* storage unavailable */ }
}
export function finishOpening(id: string) {
	try { if (pendingOpening()?.id === id) sessionStorage.removeItem(key); } catch { /* unavailable */ }
}
/** Query admission only. Never replay the create, even if the check fails. */
export async function admittedOpening(note: Opening): Promise<string | null> {
	if (!note.dispatched) return null;
	try {
		for (let attempt = 0; attempt < 8 && pendingOpening()?.id === note.id; attempt++) {
			const reply = await api.post<{ session_id: string | null }>('/sessions/with-worktree/receipt', {
				client_message_id: note.id, ...(note.project ? { project_id: note.project } : { project_v2_id: note.shared })
			});
			if (reply.session_id) return reply.session_id;
			await new Promise(done => setTimeout(done, 400));
		}
	} catch { /* old worker, offline, or no longer authorized: keep the draft */ }
	return null;
}
