import type { ActiveView } from '$lib/stores/layout.svelte';

/**
 * The chat the layout's session view should now name, or null to leave it.
 *
 * "New chat" points the layout at `{ type: 'session', sessionId: null }` and the
 * layout persists that to localStorage. When the new chat's first send creates
 * its session, the layout must follow: on boot "the layout says new chat" wins
 * over the restored session (AppPage), so a stale null threw a reload, even
 * mid-reply, back to an empty new chat (found by the reload-midstream journey).
 *
 * Only a session view naming no chat follows. Every navigation to a chat names
 * it itself (session-navigation), and "New chat" clears the open session before
 * it points the layout at null, so a null view next to a bound session is
 * always a new chat that got created.
 */
export function createdChatToFollow(view: ActiveView | null, openSessionId: string | null): string | null {
	if (!openSessionId || view?.type !== 'session') return null;
	return (view.sessionId ?? null) === null ? openSessionId : null;
}
