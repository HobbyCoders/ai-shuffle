/**
 * Whether the open chat changed while boot's first loads ran (AppPage awaits
 * profiles, sessions, projects and tags after `init()`, then reconciles the
 * layout's saved view with the session init restored).
 *
 * The composer works during those loads, so a user who types and sends right
 * after the page appears creates a chat before they finish. The reconcile's
 * "the layout says New Chat, so drop the restored session" rule was written for
 * a session INIT restored; applied to a chat the user just created it closed
 * that chat and threw its first message out of view (#811). So anything other
 * than what init left is the user's own doing and stays:
 *
 *   - a different session id: a send created it, or the user opened a chat;
 *   - no session id yet but messages on screen: a first send is waiting for
 *     its session to be created.
 *
 * `sessionAfterInit` is the open session right after init and the layout's
 * early New Chat settle (null when none was restored or New Chat won).
 */
export function chatChangedDuringBoot(
	sessionAfterInit: string | null,
	now: { sessionId: string | null; messages?: readonly unknown[] } | null | undefined
): boolean {
	const sessionId = now?.sessionId ?? null;
	if (sessionId !== sessionAfterInit) return true;
	return sessionId === null && (now?.messages?.length ?? 0) > 0;
}
