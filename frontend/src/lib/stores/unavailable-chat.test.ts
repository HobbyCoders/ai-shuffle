import { describe, expect, it, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
	unavailableChat,
	markChatUnavailable,
	clearUnavailableChat,
	unavailableChatCopy
} from './unavailable-chat';
import { pendingDeepLink } from './pending-deep-link';

describe('unavailable-chat', () => {
	beforeEach(() => {
		clearUnavailableChat();
		pendingDeepLink.set(null);
	});

	it('records the chat and status, and clears on request', () => {
		markChatUnavailable('abc123', 404);
		expect(get(unavailableChat)).toEqual({ sessionId: 'abc123', status: 404 });
		clearUnavailableChat();
		expect(get(unavailableChat)).toBeNull();
	});

	it('releases the pending deep link it answers, and only that one', () => {
		pendingDeepLink.set('abc123');
		markChatUnavailable('other');
		expect(get(pendingDeepLink)).toBe('abc123');
		markChatUnavailable('abc123');
		expect(get(pendingDeepLink)).toBeNull();
	});

	it('words a missing chat and a forbidden one differently, in plain language', () => {
		expect(unavailableChatCopy({ sessionId: 'x', status: 404 }).title).toBe('Chat not found');
		expect(unavailableChatCopy({ sessionId: 'x', status: 403 }).title).toMatch(/can’t open/);
	});
});
