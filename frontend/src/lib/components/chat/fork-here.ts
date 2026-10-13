/**
 * "Fork here" — continue a session whose owning worker is unreachable.
 *
 * The chat WebSocket sends `{type:'error', session_id, offer_fork:true}` when
 * a send cannot reach the owner; the error banner then offers this action. It
 * creates a new LOCAL session carrying the mirrored transcript and opens it.
 */

import { forkSessionHere } from '$lib/api/network';
import { getErrorMessage } from '$lib/api/client';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { notify } from '$lib/stores/notifications';

export async function forkOfferedSessionHere(sessionId: string): Promise<boolean> {
	try {
		const result = await forkSessionHere(sessionId);
		activeSession.clearError();
		activeSession.switchSession(result.session_id);
		void appData.loadSessions();
		return true;
	} catch (e: unknown) {
		notify.error(getErrorMessage(e, 'Could not fork the session on this worker'));
		return false;
	}
}
