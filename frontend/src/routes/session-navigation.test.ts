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
import { api } from '$lib/api/client';
import { openSessionLink } from '$lib/services/session-navigation';
import { resourceSync } from '$lib/services/resource-sync';

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
	it('selects a child absent from the sidebar list and starts that child feed on click', async () => {
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('parent'));
		render(SwarmCard, { sessionId: 'parent', status: 'complete', result: JSON.stringify({ swarm: {
			id: 'swarm-nav', session_id: 'parent', title: 'Navigation', status: 'completed',
			created_at: '2026-09-07', updated_at: '2026-09-07', tasks: [{
				id: 'child', agent: 'Explore', title: 'Child task', depends_on: [], status: 'completed',
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
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(event.defaultPrevented).toBe(true);
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		expect(appMode.current).toBe('code');
		expect(feedController.start).toHaveBeenLastCalledWith('worker-child');
	});

	it('lets a fresh deep link override the saved parent even when the child is not listed', async () => {
		history.replaceState(null, '', '/?session=worker-child');
		render(Page);
		await waitFor(() => expect(activeSession.current.sessionId).toBe('worker-child'));
		expect(layout.current.activeView).toEqual({ type: 'session', sessionId: 'worker-child' });
		expect(appMode.current).toBe('code');
		expect(feedController.start).toHaveBeenLastCalledWith('worker-child');
		expect(window.location.search).toBe('');
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
