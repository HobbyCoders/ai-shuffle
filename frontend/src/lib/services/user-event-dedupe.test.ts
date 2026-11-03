import { beforeEach, describe, expect, it } from 'vitest';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';

describe('row updates heard on two sockets', () => {
	beforeEach(() => resetUserEventDedupe());

	it('drops the copy from the other socket once', () => {
		const rename = { type: 'user_session_updated', session_id: 'chat', updates: { title: 'x' } };
		expect(acceptUserEvent(rename, 'chat')).toBe(true);
		expect(acceptUserEvent(rename, 'global')).toBe(false);
		expect(acceptUserEvent(rename, 'global')).toBe(true);
	});

	it('gives an old suggested-task count no socket of its own', () => {
		const count = { type: 'user_session_updated', session_id: 'chat', updates: { pending_task_chips: 1 } };
		expect(acceptUserEvent(count, 'chat')).toBe(true);
		expect(acceptUserEvent(count, 'global')).toBe(false);
	});
});
