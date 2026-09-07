import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode, normalizeSessionMode } from '$lib/stores/sessionMode.svelte';
import { layout } from '$lib/stores/layout.svelte';

/** Shared session selection for the sidebar, deep links and in-app links. */
export function openSession(sessionId: string): void {
	const session = appData.current.sessions.find((row) => row.id === sessionId);
	const worktreeInfo = session?.worktree_id || session?.worktree_branch || session?.branch ? {
		worktreeId: session.worktree_id ?? null,
		worktreeBranch: session.worktree_branch ?? session.branch ?? '',
		worktreeDeleted: session.worktree_deleted ?? false,
		worktreeIntent: 'worktree' as const,
	} : null;
	appMode.set(normalizeSessionMode(session?.mode), false);
	const select = () => {
		activeSession.switchSession(sessionId, session?.project_id, worktreeInfo,
			session?.title || null, session?.project_v2_id ?? null);
		layout.openChat(sessionId);
	};
	if (layout.current.sidebarOverlay && !layout.current.sidebarCollapsed) {
		layout.setSidebarCollapsed(true);
		requestAnimationFrame(select);
	} else {
		select();
	}
}

/** Consume ordinary link clicks in the current app; retain native new-tab gestures. */
export function openSessionLink(event: MouseEvent, sessionId: string): void {
	if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey
		|| event.shiftKey || event.altKey) return;
	event.preventDefault();
	openSession(sessionId);
}
