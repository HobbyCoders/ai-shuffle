import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
import { openSession, openSessionLink } from '$lib/services/session-navigation';
import { resourceSync } from '$lib/services/resource-sync';
import { subagentChats } from '$lib/stores/subagent-chats';
import { get } from 'svelte/store';

beforeEach(() => {
	activeSession.destroy();
	sessionStorage.clear();
	sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({
		sessionId: 'parent', title: 'Parent', mode: 'chat', profile: 'test',
	}));
	layout.setSidebarOverlay(false);
	layout.openChat('parent');
	appMode.set('chat', false);
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ tags: [], swarms: [] }), { status: 200 })));
	vi.spyOn(api, 'get').mockResolvedValue(null);
	for (const method of ['loadProfiles', 'loadSessions', 'loadProjects', 'loadClusterProjects', 'loadApiUserSessions'] as const) {
		vi.spyOn(appData, method).mockResolvedValue(undefined);
	}
	history.replaceState(null, '', '/');
});

afterEach(() => { cleanup(); activeSession.destroy(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

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

	it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }])(
		'preserves native new-tab/window navigation for %o', async (gesture) => {
			await activeSession.init();
			const event = new MouseEvent('click', { cancelable: true, button: 0, ...gesture });
			openSessionLink(event, 'worker-child');
			expect(event.defaultPrevented).toBe(false);
			expect(activeSession.current.sessionId).toBe('parent');
		},
	);
});
