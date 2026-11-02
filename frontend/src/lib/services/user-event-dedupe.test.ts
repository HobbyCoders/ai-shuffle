import { beforeEach, describe, expect, it } from 'vitest';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';

const count = (value: number, at: number, session_id = 'chat', run = 'run-a') => ({
	type: 'user_session_updated',
	session_id,
	updates: { pending_task_chips: value, pending_task_chips_seq: `${run}:${at}` }
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

	it('takes a restarted worker\'s lower numbers, whatever its clock did', () => {
		expect(acceptUserEvent(count(1, 10_000), 'chat')).toBe(true);
		expect(acceptUserEvent(count(0, 1, 'chat', 'run-b'), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 2, 'chat', 'run-b'), 'chat')).toBe(true);
	});

	it('still drops copies and late counts inside the new run', () => {
		expect(acceptUserEvent(count(1, 10_000), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 2, 'chat', 'run-b'), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 2, 'chat', 'run-b'), 'global')).toBe(false);
		expect(acceptUserEvent(count(0, 1, 'chat', 'run-b'), 'global')).toBe(false);
	});

	it('does not let a late copy from before the restart back in', () => {
		expect(acceptUserEvent(count(1, 5), 'chat')).toBe(true);
		expect(acceptUserEvent(count(0, 1, 'chat', 'run-b'), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 5), 'global')).toBe(false);
	});

	it('forgets the longest-quiet chats rather than growing without limit', () => {
		expect(acceptUserEvent(count(1, 7, 'first'), 'chat')).toBe(true);
		expect(acceptUserEvent(count(1, 7, 'active'), 'chat')).toBe(true);
		for (let chat = 0; chat < 998; chat++) acceptUserEvent(count(1, 1, `chat-${chat}`), 'chat');
		// Hearing a chat again keeps it, so the quieter one goes first.
		expect(acceptUserEvent(count(0, 8, 'active'), 'chat')).toBe(true);
		acceptUserEvent(count(1, 1, 'one-more'), 'chat');
		expect(acceptUserEvent(count(1, 7, 'first'), 'global')).toBe(true);
		expect(acceptUserEvent(count(0, 8, 'active'), 'global')).toBe(false);
	});

	it('leaves other row updates to the existing copy check', () => {
		const rename = { type: 'user_session_updated', session_id: 'chat', updates: { title: 'x' } };
		expect(acceptUserEvent(rename, 'chat')).toBe(true);
		expect(acceptUserEvent(rename, 'global')).toBe(false);
		expect(acceptUserEvent(rename, 'global')).toBe(true);
	});
});
