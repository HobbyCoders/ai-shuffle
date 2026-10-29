import { describe, expect, it } from 'vitest';
import { createdChatToFollow } from './follow-created-chat';

describe('createdChatToFollow', () => {
	it('follows a new chat once its first send created the session', () => {
		expect(createdChatToFollow({ type: 'session', sessionId: null }, 'created-by-send')).toBe('created-by-send');
		expect(createdChatToFollow({ type: 'session' } as never, 'created-by-send')).toBe('created-by-send');
	});

	it('leaves an unsent new chat alone', () => {
		expect(createdChatToFollow({ type: 'session', sessionId: null }, null)).toBeNull();
	});

	it('never overrides a view that already names a chat', () => {
		// Navigation names its chat; a mismatch is the boot reconciliation's job.
		expect(createdChatToFollow({ type: 'session', sessionId: 'a' }, 'b')).toBeNull();
		expect(createdChatToFollow({ type: 'session', sessionId: 'a' }, 'a')).toBeNull();
	});

	it('leaves Settings and other pages alone', () => {
		expect(createdChatToFollow({ type: 'settings' } as never, 'a')).toBeNull();
		expect(createdChatToFollow(null, 'a')).toBeNull();
	});
});
