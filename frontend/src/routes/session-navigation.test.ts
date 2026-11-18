import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';

vi.mock('$lib/stores/session/ws-connection', () => ({
	createWsConnection: () => ({ activate: vi.fn(), connect: vi.fn(), disconnect: vi.fn(),
		send: vi.fn(() => true), isConnected: vi.fn(() => false), destroy: vi.fn(),
		resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn() }),
}));
vi.mock('$lib/stores/session/v2/feed-controller', () => ({
	feedController: { attach: vi.fn(), start: vi.fn(), stop: vi.fn(), invalidate: vi.fn(),
		isFeeding: vi.fn(() => false), cachedPatch: vi.fn(() => null),
		hasAppliedUsage: vi.fn(() => false), isReducerStreaming: vi.fn(() => false),
		noteOptimisticSend: vi.fn(), clearOptimisticSend: vi.fn() },
}));
vi.mock('$lib/stores/auth.svelte', () => ({
	isAuthenticated: writable(true), isAdmin: writable(true),
}));
// A browser history model for shallow-routing entries (Back/Forward, #859).
const chatHistory = vi.hoisted(() => ({ entries: [{}] as Record<string, unknown>[], index: 0 }));
vi.mock('$app/navigation', async (importOriginal) => {
	const { page } = await import('$app/state');
	const write = (state: Record<string, unknown>, replace?: boolean) => {
		if (replace) chatHistory.entries[chatHistory.index] = state;
		else {
			chatHistory.entries.splice(chatHistory.index + 1, Infinity, state);
			chatHistory.index++;
		}
		page.state = state;
	};
	return {
		...(await importOriginal<object>()),
		// The web UI's shell history.
		goto: (_url: string, { state, replace }: { state: Record<string, unknown>; replace?: boolean }) => write(state, replace),
		// The phone app's own writers (main's chat links and drawer picks).
		pushState: (_url: string, state: Record<string, unknown>) => write(state),
		replaceState: (_url: string, state: Record<string, unknown>) => write(state, true),
	};
});
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/services/globalStt', () => ({ initGlobalStt: () => () => {} }));
// Keep the page's real initialization/navigation, without unrelated UI trees.
vi.mock('$lib/components/layout/AppLayout.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/voice/ActiveVoiceCallBar.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/voice/GlobalVoiceInput.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/KeyboardShortcutsModal.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/ImportSessionModal.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/TagManager.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/SpotlightSearch.svelte', () => ({ default: () => {} }));

import Page from './+page.svelte';
import SwarmCard from '$lib/components/chat/SwarmCard.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { TAB_ACTIVE_SESSION_KEY } from '$lib/stores/device-id';
import { feedController } from '$lib/stores/session/v2/feed-controller';
import { api, type Session } from '$lib/api/client';
import { openSession, openSessionLink, openSubagentLink, sessionHref } from '$lib/services/session-navigation';
import { resourceSync } from '$lib/services/resource-sync';
import { subagentChats } from '$lib/stores/subagent-chats';
import { markChatUnavailable } from '$lib/stores/unavailable-chat';
import { get } from 'svelte/store';
import { page } from '$app/state';
import type { ShellState } from '$lib/services/app-history.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { openSessionFromList } from '$lib/services/session-navigation';

beforeEach(() => {
	activeSession.destroy();
	sessionStorage.clear();
	sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({
		sessionId: 'parent', title: 'Parent', mode: 'chat', profile: 'test',
	}));
	layout.setSidebarOverlay(false);
	layout.openChat('parent');
	appMode.set('chat', false);
	subagentChats.mark('worker-child', false);
	subagentChats.mark('already-consumed', false);
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ tags: [], swarms: [] }), { status: 200 })));
	vi.spyOn(api, 'get').mockResolvedValue(null);
	for (const method of ['loadProfiles', 'loadSessions', 'loadProjects', 'loadClusterProjects', 'loadApiUserSessions'] as const) {
		vi.spyOn(appData, method).mockResolvedValue(undefined);
	}
	history.replaceState(null, '', '/');
	// Each test is a fresh load: no entry the last one wrote.
	chatHistory.entries = [{}];
	chatHistory.index = 0;
	page.state = {};
});

afterEach(() => { cleanup(); activeSession.destroy(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('session link hrefs', () => {
	it.each(['/', '/mobile/', '/mobile', '/assets/mobile-ui/'])(
		'keeps session fallback navigation on the current UI path %s', (pathname) => {
			history.replaceState(null, '', `${pathname}?old=1#old`);
			const href = sessionHref('parent / chat &+=?#雪');
			const target = new URL(href, window.location.origin);
			expect(target.origin).toBe(window.location.origin);
			expect(target.pathname).toBe(pathname);
			expect(target.searchParams.get('session')).toBe('parent / chat &+=?#雪');
			expect([...target.searchParams.keys()]).toEqual(['session']);
			expect(target.hash).toBe('');
		},
	);

	it('accepts an explicit UI pathname for server-side rendering', () => {
		vi.stubGlobal('window', undefined);
		expect(sessionHref('parent')).toBe('/?session=parent');
		expect(sessionHref('parent', '/mobile/')).toBe('/mobile/?session=parent');
	});

	it.each(['//outside.example/', 'https://outside.example/', '/\\outside.example/', '/?connect=other', '/#fragment', '/bad\npath', ''])(
		'never turns an unsafe pathname into an external or malformed href: %j', (pathname) => {
			expect(sessionHref('parent', pathname)).toBe('/?session=parent');
		},
	);
});

describe('page session refresh scheduling', () => {
	it('joins focus/visibility and root reconnect invalidations in one subscriber refresh', async () => {
		const listen = vi.spyOn(window, 'addEventListener');
		render(Page);
		await waitFor(() => expect(listen.mock.calls.some(([name]) => name === 'focus')).toBe(true));
		vi.useFakeTimers();
		const off = resourceSync.subscribe(['sessions'], () => appData.loadSessions());
		try {
			await vi.advanceTimersByTimeAsync(5000);
			vi.mocked(appData.loadSessions).mockClear();
			Object.defineProperty(document, 'visibilityState', {configurable: true, value: 'visible'});
			document.dispatchEvent(new Event('visibilitychange'));
			window.dispatchEvent(new Event('focus'));
			resourceSync.invalidate();
			expect(appData.loadSessions).not.toHaveBeenCalled();
			await vi.advanceTimersByTimeAsync(100);
			expect(appData.loadSessions).toHaveBeenCalledOnce();
		} finally { off(); resourceSync.clear(); vi.useRealTimers(); }
	});
});

describe('Swarm session navigation through the actual active-session store', () => {
	it('opens a list-hidden assistant using the returned binding, without a worktree setup intent', () => {
		const home: Session = {
			id: 'assistant-home', title: 'My assistant', hidden: true, mode: 'code',
			profile_id: 'test', status: 'completed', is_active: true,
			project_id: null, project_v2_id: null,
			total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0,
			cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0,
			turn_count: 0, tags: [], parent_session_id: null,
			fork_point_message_index: null, has_forks: false,
			created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z',
		};
		expect(appData.current.sessions.some(row => row.id === home.id)).toBe(false);
		openSession(home.id, home);
		expect(activeSession.current.sessionId).toBe(home.id);
		expect(activeSession.current.title).toBe(home.title);
		expect(activeSession.current.worktreeIntent).toBe('local');
		expect(activeSession.current.worktreeId).toBeNull();
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: home.id });
		expect(appMode.current).toBe('code');
	});
	it('marks an opened row as a subagent’s chat from is_worker, and a StartSession peer as not', () => {
		const row = (id: string, is_worker: boolean): Session => ({
			id, title: id, hidden: is_worker, mode: 'code', profile_id: 'test', status: 'completed', is_active: true,
			project_id: null, project_v2_id: null, total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0,
			cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 0, turn_count: 0, tags: [],
			parent_session_id: 'parent', fork_point_message_index: null, has_forks: false, is_worker,
			created_at: '2026-10-05T00:00:00Z', updated_at: '2026-10-05T00:00:00Z',
		});
		openSession('task-child', row('task-child', true));
		expect(get(subagentChats).has('task-child')).toBe(true);
		openSession('peer-chat', row('peer-chat', false));
		expect(get(subagentChats).has('peer-chat')).toBe(false);
		subagentChats.mark('task-child', false);
	});
	it('selects a child absent from the sidebar list and starts that child feed on click', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		render(SwarmCard, { sessionId: 'parent', status: 'complete', result: JSON.stringify({ swarm: {
			id: 'swarm-nav', session_id: 'parent', title: 'Navigation', status: 'completed',
			created_at: '2026-09-07', updated_at: '2026-09-07', tasks: [{
				id: 'child', agent: 'General Purpose', title: 'Child task', depends_on: [], status: 'completed',
				worker_session_id: 'worker-child', result: 'done',
			}],
		} }) });
		const heading = screen.getByRole('button', { name: 'Swarm Navigation' });
		if (heading.getAttribute('aria-expanded') === 'false') await fireEvent.click(heading);
		await fireEvent.click(screen.getByText('Child task'));
		const link = screen.getByRole('link', { name: /^Open / });
		expect(link.getAttribute('href')).toBe('/?session=worker-child');
		const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
		link.dispatchEvent(event);
		// A Swarm child's transcript opens as a subagent's chat (no Git or private access UI).
		expect(get(subagentChats).has('worker-child')).toBe(true);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(event.defaultPrevented).toBe(true);
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		expect(appMode.current).toBe('code');
		expect(feedController.start).toHaveBeenLastCalledWith('worker-child');
	});

	it('lets a fresh deep link override the saved parent even when the child is not listed', async () => {
		history.replaceState(null, '', '/?session=worker-child');
		render(Page);
		// The link is bound at init, before the first loads, so the saved
		// parent never shows; its lane follows once the deep link is handled
		// (the param is stripped then).
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		await waitFor(() => expect(window.location.search).toBe(''));
		expect(activeSession.current.sessionId).toBe('worker-child');
		expect(appMode.current).toBe('code');
		expect(feedController.start).toHaveBeenLastCalledWith('worker-child');
	});

	it('selects a worker after closing the mobile drawer', async () => {
		await activeSession.init();
		layout.setSidebarOverlay(true);
		layout.setSidebarCollapsed(false);
		const event = new MouseEvent('click', { cancelable: true, button: 0 });
		openSessionLink(event, 'worker-summary');
		expect(event.defaultPrevented).toBe(true);
		expect(layout.current.sidebarCollapsed).toBe(true);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-summary'));
		expect(layout.current.activeView?.sessionId).toBe('worker-summary');
		expect(feedController.start).toHaveBeenLastCalledWith('worker-summary');
	});

	it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }])(
		'preserves native new-tab/window navigation for %o', async (gesture) => {
			await activeSession.init();
			const event = new MouseEvent('click', { cancelable: true, button: 0, ...gesture });
			openSessionLink(event, 'worker-child');
			expect(event.defaultPrevented).toBe(false);
			expect(activeSession.current.sessionId).toBe('parent');
			openSubagentLink(event, 'worker-child');
			expect(event.defaultPrevented).toBe(false);
			expect(get(subagentChats).has('worker-child')).toBe(false);
		},
	);

	it('does not select or mark a child when another handler consumed the click', async () => {
		await activeSession.init();
		const event = new MouseEvent('click', { cancelable: true, button: 0 });
		event.preventDefault();
		openSubagentLink(event, 'already-consumed');
		expect(activeSession.current.sessionId).toBe('parent');
		expect(get(subagentChats).has('already-consumed')).toBe(false);
	});
});

describe('chat link history (#859)', () => {
	const click = () => new MouseEvent('click', { cancelable: true, button: 0 });
	const step = (delta: number) => {
		chatHistory.index += delta;
		page.state = chatHistory.entries[chatHistory.index];
	};
	it('makes each in-app chat link (the parent crumb) one Back step, and Forward returns', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		openSessionLink(click(), 'worker-child');
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		// The child's parent crumb.
		openSessionLink(click(), 'parent');
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		await waitFor(() => expect(chatHistory.entries.map((entry) => (entry as ShellState).shell?.place.sessionId)).toEqual(['parent', 'worker-child', 'parent']));

		step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		step(1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		step(1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
	});

	it('opens nothing for an entry that carries the same chat along (the phone drawer, Settings)', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		openSessionLink(click(), 'worker-child');
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		// The user picks another chat from the sidebar (no history entry), then
		// the phone drawer pushes an entry spreading the link's state.
		openSession('worker-summary');
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-summary'));
		page.state = { ...page.state, mobileDrawer: true };
		await Promise.resolve();
		expect(activeSession.current.sessionId).toBe('worker-summary');
	});

	const places = () => chatHistory.entries.map((entry) => {
		const place = (entry as ShellState).shell?.place;
		return place?.type === 'session' ? place.sessionId : place?.type;
	});
	/** Holds boot before its reconcile until `release` (a slow first load). */
	function slowBoot() {
		let release = () => {};
		vi.mocked(appData.loadSessions).mockReturnValue(new Promise<void>((resolve) => { release = resolve; }));
		return () => release();
	}

	it.each([
		['a Docs page', { type: 'docs' as const }, () => layout.current.activeView?.type === 'docs'],
		['another chat', { type: 'session' as const, sessionId: 'worker-child' },
			() => layout.current.activeView?.sessionId === 'worker-child' && activeSession.current.sessionId === 'worker-child'],
	])('reloads onto the place the browser entry saved (%s) from the first frame, not the layout another tab wrote', async (_name, place, shown) => {
		// This tab's entry was left on `place`; another tab since opened the
		// parent chat, which rewrote the shared layout snapshot. The address
		// still has the link this tab arrived by, read long ago.
		chatHistory.entries = [{ shell: { id: 'saved', place } }];
		page.state = chatHistory.entries[0];
		history.replaceState(null, '', '/?session=parent');
		// The sessions request is held: boot has not reconciled yet.
		const release = slowBoot();
		render(Page);
		await waitFor(() => expect(shown()).toBe(true));
		release();
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(shown()).toBe(true);
		expect(window.location.search).toBe('');
		expect(chatHistory.entries).toEqual([{ shell: { id: 'saved', place } }]);
	});

	it('keeps every place the user visits while boot loads, even when boot never finishes', async () => {
		slowBoot();
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		openSession('worker-child');
		layout.openFeaturePage('settings');
		layout.openFeaturePage('docs');
		await waitFor(() => expect(places()).toEqual(['parent', 'worker-child', 'settings', 'docs']));
		step(-1); step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
	});

	/** The layout still shows the parent; this tab's chat (the authority) is the child. Chat controls open over the arrival while boot loads. */
	async function arriveUnderControls() {
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({ sessionId: 'worker-child', title: 'Child', mode: 'chat', profile: 'test' }));
		const release = slowBoot();
		render(Page);
		await waitFor(() => expect(layout.current.activeView?.sessionId).toBe('parent'));
		await new Promise((resolve) => setTimeout(resolve));
		chatHistory.entries.push({ ...page.state, mobileControls: true });
		step(1);
		release();
		await waitFor(() => expect(layout.current.activeView?.sessionId).toBe('worker-child'));
	}
	const showsChild = async () => {
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		expect(activeSession.current.sessionId).toBe('worker-child');
	};

	it('shows boot’s view on the arrival entry once an overlay over it closes, so Back later returns there', async () => {
		await arriveUnderControls();
		step(-1); // Back closes the controls.
		await showsChild();
		layout.openFeaturePage('settings');
		await waitFor(() => expect(places()).toEqual(['worker-child', 'settings']));
		step(-1);
		await showsChild();
	});

	it('leaves the overlay’s entry to the sheet, and shows boot’s view on the arrival after Settings opened from the overlay, Back and Forward', async () => {
		await arriveUnderControls();
		const overlay = chatHistory.entries[1];
		layout.openFeaturePage('settings');
		await waitFor(() => expect(chatHistory.entries).toHaveLength(3));
		const showsSettings = async () => {
			await new Promise((resolve) => setTimeout(resolve, 20));
			expect(layout.current.activeView?.type).toBe('settings');
		};
		// The sheet's own entry shows nothing new, as on main.
		step(-1); await showsSettings();
		step(-1); await showsChild();
		step(1); await showsChild();
		step(1); await showsSettings();
		step(-1); await showsSettings();
		step(-1); await showsChild();
		expect(chatHistory.entries[1]).toBe(overlay);
		expect(places()[0]).toBe('worker-child');
	});

	it('narrow desktop with the sidebar open: a Forward before the deferred chat open wins over the Back it overtook', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		await new Promise((resolve) => setTimeout(resolve, 20));
		layout.openFeaturePage('notes');
		await waitFor(() => expect(places()).toEqual(['parent', 'notes']));
		layout.setSidebarOverlay(true);
		layout.setSidebarCollapsed(false);
		const frames: FrameRequestCallback[] = [];
		const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { frames.push(callback); return frames.length; });
		step(-1); // Back to the chat: its open waits a frame for the sidebar to close.
		await new Promise((resolve) => setTimeout(resolve));
		step(1); // Forward to Notes before that frame.
		await waitFor(() => expect(layout.current.activeView?.type).toBe('notes'));
		raf.mockRestore();
		for (const frame of frames.splice(0)) frame(0);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(layout.current.activeView?.type).toBe('notes');
		expect(places()).toEqual(['parent', 'notes']);
		expect(chatHistory.index).toBe(1);
	});

	it('narrow desktop with the sidebar open: a Forward before the deferred New Chat open wins over the Back it overtook', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		await new Promise((resolve) => setTimeout(resolve, 20));
		activeSession.newChat();
		layout.openChat(null);
		layout.openFeaturePage('notes');
		await waitFor(() => expect(places()).toEqual(['parent', null, 'notes']));
		layout.setSidebarOverlay(true);
		layout.setSidebarCollapsed(false);
		const frames: FrameRequestCallback[] = [];
		const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { frames.push(callback); return frames.length; });
		step(-1); // Back to New Chat: its open waits a frame for the sidebar to close.
		await new Promise((resolve) => setTimeout(resolve));
		expect(frames.length).toBeGreaterThan(0);
		step(1); // Forward to Notes before that frame.
		await waitFor(() => expect(layout.current.activeView?.type).toBe('notes'));
		raf.mockRestore();
		for (const frame of frames.splice(0)) frame(0);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(layout.current.activeView?.type).toBe('notes');
		expect(places()).toEqual(['parent', null, 'notes']);
		expect(chatHistory.index).toBe(2);
	});

	it('the phone app keeps main’s history: chat A, drawer pick B, drawer Notes, Back reaches A, Forward reopens B', async () => {
		platform.set('mobile');
		onTestFinished(() => platform.set('desktop'));
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		await new Promise((resolve) => setTimeout(resolve, 20));
		// The drawer pushes its own entry over the current one; dismissing it is history.back().
		const openDrawer = () => { chatHistory.entries.splice(chatHistory.index + 1, Infinity, { ...page.state, mobileDrawer: true }); step(1); };
		openDrawer();
		openSessionFromList('worker-child');
		step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		await waitFor(() => expect(chatHistory.entries).toEqual([{ chatSession: 'parent' }, { chatSession: 'worker-child' }]));
		openDrawer();
		layout.openFeaturePage('notes');
		step(-1);
		await waitFor(() => expect(layout.current.activeView?.type).toBe('notes'));
		await new Promise((resolve) => setTimeout(resolve, 20));
		// Notes adds no entry and writes nothing onto B's: main's entries only, no shell.
		expect(chatHistory.entries.slice(0, 2)).toEqual([{ chatSession: 'parent' }, { chatSession: 'worker-child' }]);
		expect(chatHistory.index).toBe(1);
		step(-1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'parent' });
		step(1);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
	});

	it('writes the chat this tab shows, not the shared layout’s, when the first step comes before boot settles', async () => {
		// Another tab left the parent in the shared layout; this tab restores the child.
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({ sessionId: 'worker-child', title: 'Child', mode: 'chat', profile: 'test' }));
		const release = slowBoot();
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(layout.current.activeView?.sessionId).toBe('parent');
		layout.openFeaturePage('settings');
		await waitFor(() => expect(places()).toEqual(['worker-child', 'settings']));
		release();
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(layout.current.activeView?.type).toBe('settings');
		step(-1);
		await showsChild();
	});

	it('follows a saved chat the server no longer has to New Chat after boot, so its first send is the chat a reload reopens', async () => {
		chatHistory.entries = [{ shell: { id: 'saved', place: { type: 'session', sessionId: 'parent' } } }];
		page.state = chatHistory.entries[0];
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		await new Promise((resolve) => setTimeout(resolve, 20));
		// The late answer: the session socket reports the chat gone.
		activeSession.newChat();
		markChatUnavailable('parent', 404);
		await waitFor(() => expect(layout.current.activeView).toEqual({ type: 'session', sessionId: null }));
		await waitFor(() => expect((chatHistory.entries[0] as ShellState).shell?.place).toEqual({ type: 'session', sessionId: null }));
		// New Chat's first send created its chat: the entry follows it, no new step.
		activeSession.switchSession('created');
		await waitFor(() => expect(places()).toEqual(['created']));
	});
});
