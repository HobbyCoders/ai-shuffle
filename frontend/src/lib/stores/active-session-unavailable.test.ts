/**
 * A chat the server will not serve (feed 404/403) leaves for a New Chat that
 * says why, instead of a silent empty screen (W4-A item 2).
 */
import { describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';

vi.mock('./session/ws-connection', () => ({
	createWsConnection: () => ({
		connect: vi.fn(),
		disconnect: vi.fn(),
		send: vi.fn(() => true),
		isConnected: vi.fn(() => false),
		resetLastMessageTime: vi.fn(),
		resetReconnectAttempts: vi.fn(),
		destroy: vi.fn(),
	}),
}));

const captured = vi.hoisted(() => ({ deps: null as null | Record<string, any> }));
vi.mock('./session/v2/feed-controller', () => ({
	feedController: {
		attach: (deps: Record<string, any>) => { captured.deps = deps; },
		start: vi.fn(),
		stop: vi.fn(),
		restart: vi.fn(),
		isFeeding: vi.fn(() => false),
		cachedPatch: vi.fn(() => null),
	},
}));

import { activeSession } from './active-session';
import { unavailableChat } from './unavailable-chat';

function openSession(id: string) {
	captured.deps!.update((s: any) => ({ ...s, sessionId: id }));
}

describe('active-session · unavailable chat notice', () => {
	it('records the chat the feed could not open and leaves it', () => {
		openSession('short-id');
		captured.deps!.onSessionUnavailable('short-id', 404);
		expect(get(activeSession).sessionId).toBeNull();
		expect(get(unavailableChat)).toEqual({ sessionId: 'short-id', status: 404 });
	});

	it('ignores a late answer for a chat the user already left', () => {
		activeSession.newChat();
		openSession('chat-now');
		captured.deps!.onSessionUnavailable('chat-before', 403);
		expect(get(activeSession).sessionId).toBe('chat-now');
		expect(get(unavailableChat)).toBeNull();
	});

	it('forgets the notice when the user starts a new chat', () => {
		openSession('gone');
		captured.deps!.onSessionUnavailable('gone', 404);
		expect(get(unavailableChat)).not.toBeNull();
		activeSession.newChat();
		expect(get(unavailableChat)).toBeNull();
	});
});
