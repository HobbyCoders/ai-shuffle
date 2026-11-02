import { beforeEach, describe, expect, it } from 'vitest';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';

const count = (value: number, at: number, session_id = 'chat') => ({
	type: 'user_session_updated',
	session_id,
	updates: { pending_task_chips: value, pending_task_chips_at: at }
});

/** What a row ends up showing after these deliveries, in this order. */
function replay(deliveries: Array<['chat' | 'global', ReturnType<typeof count>]>): number | null {
	let shown: number | null = null;
	for (const [source, event] of deliveries) {
		if (acceptUserEvent(event, source)) shown = event.updates.pending_task_chips;
	}
	return shown;
}

describe('task chip counts heard on two sockets', () => {
	beforeEach(() => resetUserEventDedupe());

	const updates = [count(1, 1), count(0, 2), count(1, 3), count(0, 4), count(1, 5)];
	const [one, two, three, four, five] = updates;

	it('keeps the newest count when the slower socket replays a repeated value', () => {
		expect(replay([
			['chat', one], ['chat', two], ['chat', three],
			['global', one], ['global', two],
			['chat', four],
			['global', three], ['global', four]
		])).toBe(0);
	});

	it('does not lose a waiting chip to a late copy of an older zero', () => {
		expect(replay([
			['chat', one], ['chat', two], ['chat', three],
			['global', one], ['global', two], ['global', three]
		])).toBe(1);
		expect(replay([['chat', four], ['chat', five], ['global', four], ['global', five]])).toBe(1);
	});

	it('takes each change once, whichever socket says it first', () => {
		expect(acceptUserEvent(one, 'global')).toBe(true);
		expect(acceptUserEvent(one, 'chat')).toBe(false);
		expect(acceptUserEvent(two, 'chat')).toBe(true);
		expect(acceptUserEvent(two, 'global')).toBe(false);
	});

	it('keeps each chat apart', () => {
		expect(acceptUserEvent(count(1, 9, 'a'), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 3, 'b'), 'chat')).toBe(true);
	});

	it('leaves other row updates to the existing copy check', () => {
		const rename = { type: 'user_session_updated', session_id: 'chat', updates: { title: 'x' } };
		expect(acceptUserEvent(rename, 'chat')).toBe(true);
		expect(acceptUserEvent(rename, 'global')).toBe(false);
		expect(acceptUserEvent(rename, 'global')).toBe(true);
	});
});
