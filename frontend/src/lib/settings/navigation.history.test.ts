/**
 * Settings and chat links share one browser history. A fake shallow-routing
 * stack stands in for SvelteKit's pushState/replaceState, and Back or Forward
 * replays an entry through both followers, as SettingsShell and AppPage do.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { writable, get } from 'svelte/store';
import { page } from '$app/state';

type Entry = Record<string, unknown>;
const h = vi.hoisted(() => ({ stack: [] as Record<string, unknown>[], index: 0 }));
const active = vi.hoisted(() => ({ store: null as unknown as ReturnType<typeof writable<{ sessionId: string | null }>> }));

vi.mock('$app/navigation', async () => {
	const { page } = await import('$app/state');
	return {
		goto: vi.fn(),
		pushState: (_url: string, state: Entry) => {
			h.stack = [...h.stack.slice(0, h.index + 1), state];
			h.index = h.stack.length - 1;
			page.state = state;
		},
		replaceState: (_url: string, state: Entry) => {
			h.stack[h.index] = state;
			page.state = state;
		}
	};
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	active.store = writable<{ sessionId: string | null }>({ sessionId: null });
	return {
		activeSession: {
			subscribe: active.store.subscribe,
			switchSession: (id: string) => active.store.set({ sessionId: id })
		}
	};
});
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		activeView: writable(null),
		layout: {
			current: { sidebarOverlay: false, sidebarCollapsed: true },
			openChat: vi.fn(), setSidebarCollapsed: vi.fn(), openFeaturePage: vi.fn(), clearFeatureItemId: vi.fn()
		}
	};
});
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { current: { sessions: [] } } }));

import { followChatHistory, openSession, openSessionFromList } from '$lib/services/session-navigation';
import { settingsNav } from './navigation.svelte';

/** Land on an entry the way the app does; a stale entry can move again. */
function land(delta: number, settingsOnScreen = true) {
	h.index += delta;
	page.state = h.stack[h.index];
	settingsNav.followHistory(page.state as never, settingsOnScreen);
	followChatHistory(page.state);
}
const openChat = () => get(active.store).sessionId;
const here = () => page.state as Entry;

beforeEach(() => {
	h.stack = [{}];
	h.index = 0;
	page.state = {};
	active.store.set({ sessionId: null });
	settingsNav.reset();
	sessionStorage.clear();
	vi.spyOn(history, 'go').mockImplementation((delta = 0) => land(delta));
});

describe('Settings and chat in one history', () => {
	/** Chat A, a phone-width Settings page, then a sidebar pick of chat B. */
	function chatBOverSettingsPage() {
		openSession('chat-a');
		settingsNav.stacked = true;
		settingsNav.selectCategory('security');
		openSessionFromList('chat-b');
		settingsNav.followHistory(here() as never, false);
		expect(h.stack.map((entry) => entry.chatSession)).toEqual([undefined, 'chat-a', 'chat-b']);
		expect(here().settingsView).toBe('page'); // the copy this rule exists for
	}

	it('narrowing on a chat entry leaves it alone, so Back and Forward still reach both chats', () => {
		chatBOverSettingsPage();
		const chatEntry = { ...here() };

		settingsNav.stacked = false;
		settingsNav.selectCategory('cloud');
		settingsNav.stacked = true;
		settingsNav.startStack(here() as never);

		expect(h.stack[2]).toEqual(chatEntry);
		expect(h.stack).toHaveLength(4);
		expect(here()).toMatchObject({ settingsView: 'page', settingsCategory: 'cloud', settingsDepth: 1 });
		expect(here().chatSession).toBeUndefined();

		land(-1); // off the new Settings page, onto chat B's entry: the list
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.depth).toBe(0);
		expect(openChat()).toBe('chat-b');

		land(-1);
		expect(openChat()).toBe('chat-a');
		land(+1);
		expect(openChat()).toBe('chat-b');
	});

	it('a chat entry is never read as a Settings position when Settings is on screen', () => {
		chatBOverSettingsPage();
		settingsNav.followHistory(here() as never);
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.depth).toBe(0);
	});

	it('a reload on a chat entry does not adopt the Settings position it copied', () => {
		chatBOverSettingsPage();
		settingsNav.selectCategory('security');
		h.stack.length = 3;
		h.index = 2;
		page.state = h.stack[2];
		settingsNav.startStack(here() as never);
		expect(h.stack).toHaveLength(4);
		expect(settingsNav.depth).toBe(1);
	});

	it('the phone drawer over a Settings page keeps the reader on that page', () => {
		settingsNav.stacked = true;
		settingsNav.selectCategory('security');
		settingsNav.followHistory({ ...here(), mobileDrawer: true } as never);
		expect(settingsNav.view).toBe('page');
		expect(settingsNav.depth).toBe(1);
	});

	it('never rewrites an entry Settings left behind: it adds one above and Back skips the old', () => {
		settingsNav.stacked = true;
		settingsNav.selectCategory('security');
		settingsNav.selectCategory('voice');
		const left = h.stack.map((entry) => ({ ...entry }));

		settingsNav.stacked = false;
		settingsNav.selectCategory('cloud');
		settingsNav.stacked = true;
		settingsNav.startStack(here() as never);

		expect(h.stack.slice(0, left.length)).toEqual(left);
		expect(here()).toMatchObject({ settingsCategory: 'cloud', settingsDepth: 3, settingsStaleBelow: 3 });

		land(-1);
		expect(h.index).toBe(0);
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.category).toBe('cloud');
	});

	it('a second round of widening and narrowing still skips everything left below', () => {
		settingsNav.stacked = true;
		settingsNav.selectCategory('security');
		for (const category of ['voice', 'cloud'] as const) {
			settingsNav.stacked = false;
			settingsNav.depth = 0;
			settingsNav.selectCategory(category);
			settingsNav.stacked = true;
			settingsNav.startStack(here() as never);
		}
		expect(h.stack.map((entry) => entry.settingsCategory)).toEqual([undefined, 'security', 'voice', 'cloud']);

		land(-1);
		expect(h.index).toBe(0);
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.category).toBe('cloud');
	});

	it('Settings closed on a leftover entry: the next pick still skips it on Back', () => {
		settingsNav.stacked = true;
		settingsNav.selectCategory('security');
		settingsNav.forgetPlace();
		const reopened = here();
		settingsNav.view = 'index';

		settingsNav.startStack(reopened as never);
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.depth).toBe(1);

		settingsNav.selectCategory('cloud');
		expect(here()).toMatchObject({ settingsCategory: 'cloud', settingsDepth: 2 });
		land(-1);
		expect(h.index).toBe(0);
		expect(settingsNav.view).toBe('index');
	});
});
