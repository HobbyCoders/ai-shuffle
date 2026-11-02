import { beforeEach, describe, expect, it } from 'vitest';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';

const count = (value: number) => ({
	type: 'user_session_updated',
	session_id: 'chat',
	updates: { pending_task_chips: value }
});

/** What a row ends up showing after these deliveries, in this order. */
function replay(deliveries: Array<['chat' | 'global', number]>): number | null {
	let shown: number | null = null;
	for (const [source, value] of deliveries) {
		if (acceptUserEvent(count(value), source)) shown = value;
	}
	return shown;
}

describe('task chip counts heard on two sockets', () => {
	beforeEach(() => resetUserEventDedupe());

	it('keeps the newest count when the chat socket runs ahead', () => {
		expect(replay([
			['chat', 1], ['chat', 0], ['chat', 1],
			['global', 1], ['global', 0],
			['chat', 0],
			['global', 1], ['global', 0]
		])).toBe(0);
	});

	it('does not lose a waiting chip to a late copy on the chat socket', () => {
		expect(replay([['global', 1], ['global', 0], ['global', 1], ['chat', 1], ['chat', 0]])).toBe(1);
	});

	it('takes a repeated value from the global socket, since a count can return', () => {
		expect(replay([['global', 1], ['global', 0], ['global', 1], ['global', 1]])).toBe(1);
	});

	it('leaves a worker restart to the global socket\'s reconnect, not the chat socket', () => {
		// The chat socket reconnected first and says 1 while the old global socket
		// drains its last 0. The row shows 0 until the global socket's reconnect
		// reloads the list (live-sync.ts), which is what corrects it.
		expect(replay([['chat', 1], ['global', 0]])).toBe(0);
	});

	it('leaves other row updates to the existing copy check', () => {
		const rename = { type: 'user_session_updated', session_id: 'chat', updates: { title: 'x' } };
		expect(acceptUserEvent(rename, 'chat')).toBe(true);
		expect(acceptUserEvent(rename, 'global')).toBe(false);
		expect(acceptUserEvent(rename, 'global')).toBe(true);
	});
});
