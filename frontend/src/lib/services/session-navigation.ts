import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode, normalizeSessionMode } from '$lib/stores/sessionMode.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { archivedChats } from '$lib/stores/archived-chats';
import { subagentChats } from '$lib/stores/subagent-chats';
import type { Session } from '$lib/api/client';
import { pushState, replaceState } from '$app/navigation';
import { ignoreRouterNotReady } from '$lib/utils/shallow-history';
import { page } from '$app/state';
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
	// A subagent's chat opens without Git or Vault controls.
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

/** What a chat link's history entry carries in `page.state`. */
type ChatHistoryState = { chatSession?: string };
/** The chat the current history entry stands for, as this module last set or followed it. */
let historyChat: string | null = null;

/**
 * A chat pick waiting for the phone drawer's own entry to be popped: its push,
 * and the timer that gives up on a Back the router never reports.
 */
let afterDrawerPop: { push: (gaveUp?: boolean) => void; giveUp: ReturnType<typeof setTimeout> } | null = null;

function takeDrawerWait(): ((gaveUp?: boolean) => void) | null {
	if (!afterDrawerPop) return null;
	const { push, giveUp } = afterDrawerPop;
	afterDrawerPop = null;
	clearTimeout(giveUp);
	return push;
}

/** Make a chat link one Back step: mark the current entry with the chat being left, then push the target. */
function pushChatHistory(sessionId: string): void {
	const from = get(activeSession).sessionId;
	if (!from || from === sessionId) return;
	const push = (gaveUp = false) => {
		const { mobileDrawer, ...rest } = (page.state ?? {}) as App.PageState & ChatHistoryState & { mobileDrawer?: boolean };
		// Gave up waiting: `page.state` may still be the drawer's entry, and
		// its marker must not be copied onto chat entries.
		const base: ChatHistoryState & Record<string, unknown> = gaveUp || !mobileDrawer ? rest : { ...rest, mobileDrawer };
		try {
			// Both are queued in this order and land in it (SvelteKit 3 applies them asynchronously).
			if (base.chatSession !== from) ignoreRouterNotReady(replaceState('', { ...base, chatSession: from } as App.PageState));
			ignoreRouterNotReady(pushState('', { ...base, chatSession: sessionId } as App.PageState));
			historyChat = sessionId;
		} catch {
			// Router not ready (tests, early boot): the link still opens the chat.
		}
	};
	// A pick still waiting keeps its place: its entry goes in first.
	const earlier = takeDrawerWait();
	const run = earlier ? (gaveUp?: boolean) => { earlier(gaveUp); push(gaveUp); } : push;
	// The phone drawer's own entry is being popped (its dismissal calls
	// history.back(), which lands later). Push after that Back lands, or the
	// pending Back would step off the new chat entry again. SvelteKit updates
	// `page.state` some time after `popstate`, so the wait ends when
	// followChatHistory is handed a state without the drawer, never on the
	// event or on elapsed time: pushing earlier would copy the drawer's marker
	// onto both chat entries.
	if ((page.state as { mobileDrawer?: boolean } | undefined)?.mobileDrawer && typeof window !== 'undefined') {
		afterDrawerPop = {
			push: run,
			// The router never reported the Back (a newer navigation cancelled
			// its update): the pick still gets its entry.
			giveUp: setTimeout(() => takeDrawerWait()?.(true), DRAWER_BACK_WAIT_MS)
		};
		return;
	}
	run();
}
/** How long a chat pick waits for the router to report the phone drawer's Back before pushing anyway. */
const DRAWER_BACK_WAIT_MS = 600;

/**
 * Called by AppPage whenever `page.state` changes: a Back/Forward step onto
 * a chat link's entry opens that chat. Other entries (Settings, the phone
 * drawer) carry the same chat along or none at all, so they open nothing.
 */
export function followChatHistory(state: unknown): void {
	if (afterDrawerPop && !(state as { mobileDrawer?: boolean } | undefined)?.mobileDrawer) {
		// The drawer's entry is gone and this is the entry under it: the pick
		// in progress decides which chat is open, not the entry being left.
		takeDrawerWait()?.();
		return;
	}
	const id = (state as ChatHistoryState | undefined)?.chatSession;
	if (!id || id === historyChat) return;
	historyChat = id;
	if (get(activeSession).sessionId !== id) openSession(id);
}

/**
 * A chat picked from a chat list (the sidebar, the phone drawer): one Back
 * step returns to the chat being left, Forward opens the pick again (#953).
 * Picking the chat already open, or opening one from New chat, adds no entry.
 */
export function openSessionFromList(sessionId: string): void {
	pushChatHistory(sessionId);
	openSession(sessionId);
}

/** Consume ordinary link clicks in the current app; retain native new-tab gestures. */
export function openSessionLink(event: MouseEvent, sessionId: string): void {
	if (!ordinarySessionClick(event)) return;
	event.preventDefault();
	pushChatHistory(sessionId);
	openSession(sessionId);
}
