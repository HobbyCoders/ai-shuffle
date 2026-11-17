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

import { openSession } from './session-navigation';

// Back/Forward through the real layout and chat store is covered in routes/session-navigation.test.ts.
describe('reopening the chat already open (#1350)', () => {
	it('tells the store it is a reopen; any other open is navigation', () => {
		active.switches = [];
		openSession('chat-a', undefined, { reopen: true });
		openSession('chat-b');
		expect(active.switches.map(call => [call[0], call[5]])).toEqual([
			['chat-a', { reopen: true }],
			['chat-b', undefined],
		]);
		// An ordinary open calls the store with the five arguments it always had.
		expect(active.switches[1]).toHaveLength(5);
	});
});
