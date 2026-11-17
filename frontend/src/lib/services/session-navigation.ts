import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode, normalizeSessionMode } from '$lib/stores/sessionMode.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { archivedChats } from '$lib/stores/archived-chats';
import { subagentChats } from '$lib/stores/subagent-chats';
import type { Session } from '$lib/api/client';
import { get } from 'svelte/store';

/** A same-origin deep link whose fallback stays in the current desktop or phone UI. */
export function sessionHref(sessionId: string, pathname: string = typeof window === 'undefined' ? '/' : window.location.pathname): string {
	// Accept pathnames only, not origins, protocol-relative URLs, queries or fragments.
	const path = pathname.startsWith('/') && !pathname.startsWith('//') && !/[\\?#\x00-\x20\x7f]/.test(pathname)
		? pathname : '/';
	return `${path}?session=${encodeURIComponent(sessionId)}`;
}

function ordinarySessionClick(event: MouseEvent): boolean {
	return !event.defaultPrevented && event.button === 0 && !event.ctrlKey && !event.metaKey
		&& !event.shiftKey && !event.altKey;
}

/** Shared session selection for the sidebar, deep links and in-app links. */
/** Leave a full-page view for the chat that was open, or new chat when none was. */
export function backToChats(): void {
	const sessionId = get(activeSession).sessionId;
	if (sessionId) openSession(sessionId);
	else layout.openChat(null);
}

/**
 * `reopen`: the chat is already open and this only reloads it with its row
 * (boot after a `/?session=` link). Not a navigation, so a send waiting on
 * that chat goes on (#1350). A different chat by then is opened as usual.
 */
export function openSession(sessionId: string, openedSession?: Session, { reopen = false }: { reopen?: boolean } = {}): void {
	// Persistent assistant homes are intentionally absent from ordinary lists.
	const session = openedSession ?? appData.current.sessions.find((row) => row.id === sessionId);
	const worktreeInfo = session?.worktree_id || session?.worktree_branch || session?.branch ? {
		worktreeId: session.worktree_id ?? null,
		worktreeBranch: session.worktree_branch ?? session.branch ?? '',
		worktreeDeleted: session.worktree_deleted ?? false,
		worktreeIntent: 'worktree' as const,
	} : null;
	appMode.set(normalizeSessionMode(session?.mode), false);
	// An archived chat opens read-only; /live corrects a row this tab lacks.
	if (session) archivedChats.mark(sessionId, session.status === 'archived');
	// A subagent's chat opens without Git or private access controls.
	if (session?.is_worker !== undefined) subagentChats.mark(sessionId, session.is_worker);
	const select = () => {
		// Only a reopen says so; every other open calls the store as before.
		const place = [sessionId, session?.project_id, worktreeInfo,
			session?.title || null, session?.project_v2_id ?? null] as const;
		if (reopen) activeSession.switchSession(...place, { reopen: true });
		else activeSession.switchSession(...place);
		layout.openChat(sessionId);
	};
	if (layout.current.sidebarOverlay && !layout.current.sidebarCollapsed) {
		layout.setSidebarCollapsed(true);
		requestAnimationFrame(select);
	} else {
		select();
	}
}

/** Open a Task or Swarm child's transcript, known to be a subagent's chat before /live answers. */
export function openSubagentLink(event: MouseEvent, sessionId: string): void {
	if (!ordinarySessionClick(event)) return;
	subagentChats.mark(sessionId, true);
	openSessionLink(event, sessionId);
}

/**
 * A chat picked from a chat list (the sidebar, the phone drawer): one Back
 * step returns to the chat being left, Forward opens the pick again (#953).
 * Picking the chat already open, or opening one from New chat, adds no entry.
 */
export function openSessionFromList(sessionId: string): void {
	openSession(sessionId);
}

/** Consume ordinary link clicks in the current app; retain native new-tab gestures. */
export function openSessionLink(event: MouseEvent, sessionId: string): void {
	if (!ordinarySessionClick(event)) return;
	event.preventDefault();
	openSession(sessionId);
}
