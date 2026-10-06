import { describe, expect, it } from 'vitest';
import { classifyChatSurface } from '$lib/components/layout/chat-empty-state';
import { deepLinkLoading, deepLinkSessionId } from './pending-deep-link';

describe('pending deep link', () => {
	it('reads the session id from the boot URL', () => {
		expect(deepLinkSessionId('?session=abc-123')).toBe('abc-123');
		expect(deepLinkSessionId('?session=')).toBeNull();
		expect(deepLinkSessionId('')).toBeNull();
	});

	it('presents an unbound chat as loading, not New Chat, while the link is pending', () => {
		const surface = (pending: string | null, sessionId: string | null) =>
			classifyChatSurface({
				sessionId,
				messageCount: 0,
				isRestoringSession: false,
				isLoadingSession: deepLinkLoading(pending, sessionId),
			});
		// Boot has read the link; init and the first loads are still running.
		expect(surface('abc', null)).toBe('loading');
		// A restored session that is not the link's target is about to be replaced.
		expect(surface('abc', 'other')).toBe('loading');
		// No link: the ordinary New Chat hero.
		expect(surface(null, null)).toBe('new');
		// Once the linked session is bound, its own loading flags take over.
		expect(deepLinkLoading('abc', 'abc')).toBe(false);
	});
});
