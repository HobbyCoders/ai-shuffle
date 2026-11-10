/**
 * #953: a chat picked from the sidebar is one Back step. A fake shallow-routing
 * history stack stands in for SvelteKit's pushState/replaceState, and Back or
 * Forward replays an entry's state through followChatHistory, as AppPage does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable, get } from 'svelte/store';
import { page } from '$app/state';

type Entry = Record<string, unknown>;
const h = vi.hoisted(() => ({ stack: [] as Record<string, unknown>[], index: 0 }));
const active = vi.hoisted(() => ({ store: null as unknown as ReturnType<typeof writable<{ sessionId: string | null }>>, switches: [] as unknown[][] }));

vi.mock('$app/navigation', async () => {
	const { page } = await import('$app/state');
	return {
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
			switchSession: (id: string, ...rest: unknown[]) => { active.switches.push([id, ...rest]); active.store.set({ sessionId: id }); }
		}
	};
});
vi.mock('$lib/stores/layout.svelte', () => ({
	layout: { current: { sidebarOverlay: false, sidebarCollapsed: true }, openChat: vi.fn(), setSidebarCollapsed: vi.fn() }
}));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { current: { sessions: [] } } }));

import { followChatHistory, openSession, openSessionFromList } from './session-navigation';

function step(delta: number) {
	h.index += delta;
	page.state = h.stack[h.index];
	followChatHistory(page.state);
}
const openChat = () => get(active.store).sessionId;

beforeEach(() => {
	h.stack = [{}];
	h.index = 0;
	page.state = {};
	active.store.set({ sessionId: null });
});
afterEach(() => vi.useRealTimers());

describe('sidebar chat switches and browser history (#953)', () => {
	it('Back after a sidebar switch A → B opens A in place, Forward opens B again', () => {
		openSession('chat-a'); // boot / deep-link restore: no entry
		expect(h.stack).toHaveLength(1);
		openSessionFromList('chat-b');
		expect(openChat()).toBe('chat-b');
		expect(h.stack).toHaveLength(2);

		step(-1);
		expect(openChat()).toBe('chat-a');
		step(+1);
		expect(openChat()).toBe('chat-b');
	});

	it('picking the open chat or opening one from New chat adds no entry', () => {
		openSessionFromList('chat-a'); // from New chat
		expect(h.stack).toHaveLength(1);
		openSessionFromList('chat-a'); // already open
		expect(h.stack).toHaveLength(1);
		expect(openChat()).toBe('chat-a');
	});

	it('a pick from the phone drawer pushes only after the drawer entry is popped', () => {
		vi.useFakeTimers();
		openSession('chat-a');
		// The drawer opened with its own entry, and its dismissal called history.back().
		h.stack = [{}, { mobileDrawer: true }];
		h.index = 1;
		page.state = h.stack[1];
		openSessionFromList('chat-b');
		expect(openChat()).toBe('chat-b');
		expect(h.stack).toHaveLength(2); // nothing pushed on top of the drawer entry

		// The pending Back lands on the chat entry under the drawer.
		h.index = 0;
		page.state = h.stack[0];
		window.dispatchEvent(new PopStateEvent('popstate'));
		return Promise.resolve().then(() => {
			expect(h.stack).toEqual([{ chatSession: 'chat-a' }, { chatSession: 'chat-b' }]);
			step(-1);
			expect(openChat()).toBe('chat-a');
		});
	});
});

describe('reopening the chat already open (#1350)', () => {
	it('tells the store it is a reopen; any other open is navigation', () => {
		active.switches = [];
		openSession('chat-a', undefined, { reopen: true });
		openSession('chat-b');
		expect(active.switches.map(call => [call[0], call.at(-1)])).toEqual([
			['chat-a', { reopen: true }],
			['chat-b', { reopen: false }],
		]);
	});
});
