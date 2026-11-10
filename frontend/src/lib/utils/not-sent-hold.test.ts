import { describe, expect, it } from 'vitest';
import type { ChatMessage, QueuedMessage } from '$lib/stores/types';
import {
	createNotSentHold,
	createNotSentRowsArrival,
	createStopBubbleHold,
	holdNotSentRows,
	nameStoppedTurn,
	nameStopRecovery,
	stopRecoveryAppliesToTurn,
	restingOnNotSentRows,
	STOP_TURN_NOT_NAMED,
	stopPressedOn,
	withHeldBubbles
} from './not-sent-hold';

const reply: ChatMessage = { id: 'a', role: 'assistant', type: 'text', content: 'reply' };
const row = (id: string, duringTurn?: string): ChatMessage => ({
	id,
	role: 'user',
	type: 'text',
	content: `words ${id}`,
	queuedTurnId: `turn-${id}`,
	metadata: { not_sent: true, not_sent_id: id, ...(duringTurn ? { not_sent_during_turn: duringTurn } : {}) },
});

describe('holdNotSentRows', () => {
	it('shows rows as bubbles while the turn Stop interrupted is still live', () => {
		const held = holdNotSentRows([reply, row('1', 'live'), row('2', 'live')], 'live');
		expect(held.messages).toEqual([reply]);
		expect(held.bubbles.map(b => [b.id, b.content, b.status])).toEqual([
			['turn-1', 'words 1', 'stopped'],
			['turn-2', 'words 2', 'stopped'],
		]);
	});

	it('keeps the attachment chips of the queued bubble it replaces', () => {
		const withFile: ChatMessage = { ...row('1', 'live'), metadata: { ...row('1', 'live').metadata, upload_paths: ['/uploads/a.png'] } };
		const held = holdNotSentRows([reply, withFile, row('2', 'live')], 'live');
		expect(held.bubbles.map(b => b.uploadPaths)).toEqual([['/uploads/a.png'], undefined]);
	});

	it('hands every row over together when the turn ends', () => {
		const messages = [reply, row('1', 'live'), row('2', 'live')];
		const held = holdNotSentRows(messages, null);
		expect(held.messages).toBe(messages);
		expect(held.bubbles).toEqual([]);
	});

	it('does not hold a row for a later turn, or one written with no turn running', () => {
		const messages = [reply, row('1', 'old'), row('2')];
		expect(holdNotSentRows(messages, 'next').messages).toBe(messages);
	});

	it('leaves ordinary messages in place around a held row', () => {
		const later: ChatMessage = { id: 'b', role: 'assistant', type: 'text', content: 'more' };
		expect(holdNotSentRows([reply, row('1', 'live'), later], 'live').messages).toEqual([reply, later]);
	});
});

describe('createNotSentHold', () => {
	const waiting = (id: string) => ({ id, content: id, queuedAt: 0 });

	it('keeps the rows as bubbles past turn end while a later message waits below them', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		expect(hold(messages, 'live', [waiting('later')]).bubbles).toHaveLength(1);
		const carried = hold(messages, null, [waiting('later')]);
		expect(carried.bubbles).toHaveLength(1);
		expect(carried.messages).toEqual([reply]);
		expect(hold(messages, null, []).messages).toBe(messages);
	});

	it('keeps bubbles until Stop has also ended the running background work', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live'), row('2', 'live')];
		const queued: QueuedMessage[] = [];
		hold(messages, 'live', queued, true);
		// turn.aborted and terminal task.updated are separate feed events.
		const ending = hold(messages, null, queued, true);
		expect(ending.messages).toEqual([reply]);
		expect(ending.bubbles).toHaveLength(2);
		// Only the task flag changes: it must invalidate the memoized hold too.
		// This update removes RunningTasksList: only now may taller rows appear.
		const settled = hold(messages, null, queued, false);
		expect(settled.messages).toBe(messages);
		expect(settled.recoverAt).toBeUndefined();
	});

	it('releases stopped words within ten seconds even if a task survives Stop', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		const queued: QueuedMessage[] = [];
		hold(messages, 'live', queued, true, 1000);
		const settling = hold(messages, null, queued, true, 1000);
		expect(settling.bubbles).toHaveLength(1);
		expect(settling.recoverAt).toBe(11_000);
		expect(hold(messages, null, queued, true, 10_999).bubbles).toHaveLength(1);
		// No event, task flag or array changes: only the recovery clock advances.
		expect(hold(messages, null, queued, true, 11_000).messages).toBe(messages);
		expect(hold(messages, null, queued, true, 12_000).bubbles).toEqual([]);
	});

	it('does not extend task-settlement recovery when more feed updates arrive', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		hold(messages, 'live', [], true, 1000);
		hold(messages, null, [], true, 1000);
		const updated = [...messages];
		expect(hold(updated, null, [], true, 10_000).bubbles).toHaveLength(1);
		expect(hold(updated, null, [], true, 11_000).messages).toBe(updated);
	});

	it('does not start a background-work hold for rows already shown', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		expect(hold(messages, null, [], true).messages).toBe(messages);
		hold(messages, 'live', [], true);
		expect(hold(messages, null, [], false).messages).toBe(messages);
		expect(hold(messages, null, [], true).messages).toBe(messages);
	});

	it('releases the background-work hold for recovery or a successor turn', () => {
		const messages = [reply, row('1', 'live')];
		for (const next of [null, 'next']) {
			const hold = createNotSentHold();
			hold(messages, 'live', [], true);
			hold(messages, null, [], true);
			expect(hold(messages, next, [], false).messages).toBe(messages);
		}
	});

	it('does not count a late copy of a held message as one waiting', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		const { bubbles } = hold(messages, 'live', []);
		expect(hold(messages, null, [waiting(bubbles[0].id)]).messages).toBe(messages);
	});

	it('never turns rows already showing back into bubbles', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'earlier')];
		expect(hold(messages, null, [waiting('later')]).messages).toBe(messages);
		const stopped = [reply, row('1', 'live')];
		hold(stopped, 'live', []);
		expect(hold(stopped, null, []).messages).toBe(stopped);
		expect(hold(stopped, null, [waiting('later')]).messages).toBe(stopped);
	});

	it('lets go as soon as the next turn is live', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		hold(messages, 'live', [waiting('later')]);
		hold(messages, null, [waiting('later')]);
		expect(hold(messages, 'next', [waiting('later')]).messages).toBe(messages);
	});

	it('keeps the same transcript array until its input changes', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		const first = hold(messages, 'live');
		expect(hold(messages, 'live').messages).toBe(first.messages);
		expect(hold(messages, null).messages).toBe(messages);
	});
});

describe('a Stop pressed before the feed named its turn', () => {
	it('counts as pressed from the moment it leaves, through the timeout', () => {
		expect(stopPressedOn(STOP_TURN_NOT_NAMED, null)).toBe(true);
	});

	it('binds to the turn the feed names, so the next turn was never stopped', () => {
		const named = nameStoppedTurn(STOP_TURN_NOT_NAMED, 'turn-1', true);
		expect(named).toBe('turn-1');
		expect(stopPressedOn(named, 'turn-1')).toBe(true);
		expect(stopPressedOn(named, 'turn-2')).toBe(false);
		expect(stopPressedOn(named, null)).toBe(false);
	});

	it('waits while the feed has not named one, and ends with the turn', () => {
		expect(nameStoppedTurn(STOP_TURN_NOT_NAMED, null, true)).toBe(STOP_TURN_NOT_NAMED);
		expect(nameStoppedTurn(STOP_TURN_NOT_NAMED, null, false)).toBeNull();
	});

	it('leaves a Stop on a named turn alone', () => {
		expect(nameStoppedTurn('turn-1', 'turn-2', true)).toBe('turn-1');
		expect(nameStoppedTurn('turn-1', null, false)).toBe('turn-1');
		expect(nameStoppedTurn(null, 'turn-2', true)).toBeNull();
	});
});

describe('Stop timeout recovery', () => {
	it('keeps an unnamed timeout through quiet folds and binds it to the delayed start', () => {
		let recovery = nameStopRecovery(STOP_TURN_NOT_NAMED, null);
		expect(recovery).toBe(STOP_TURN_NOT_NAMED);
		expect(stopRecoveryAppliesToTurn(recovery, null)).toBe(true);
		recovery = nameStopRecovery(recovery, 'turn-1');
		expect(recovery).toBe('turn-1');
		expect(stopRecoveryAppliesToTurn(recovery, 'turn-1')).toBe(true);
	});

	it('releases held rows permanently even if a later queue message waits below them', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'turn-1')];
		const queued = [{ id: 'later', content: 'later', queuedAt: 0 }];
		expect(hold(messages, 'turn-1', queued).bubbles).toHaveLength(1);
		const render = (recovery: string, live: string | null) => {
			const recovering = stopRecoveryAppliesToTurn(recovery, live);
			return hold(messages, recovering ? null : live, recovering ? [] : queued);
		};
		expect(render(STOP_TURN_NOT_NAMED, null).messages).toBe(messages);
		expect(render(nameStopRecovery(STOP_TURN_NOT_NAMED, 'turn-1')!, 'turn-1').messages).toBe(messages);
		expect(render('turn-1', null).messages).toBe(messages);
	});

	it('never applies the previous timeout to a started successor or its hold', () => {
		expect(stopRecoveryAppliesToTurn('turn-1', 'turn-2')).toBe(false);
		expect(nameStopRecovery('turn-1', 'turn-2')).toBeNull();
		const hold = createNotSentHold();
		const messages = [reply, row('2', 'turn-2')];
		const queued = [{ id: 'later', content: 'later', queuedAt: 0 }];
		expect(hold(messages, 'turn-2', queued).bubbles).toHaveLength(1);
		const recovery = nameStopRecovery('turn-1', 'turn-2');
		expect(hold(messages, null, stopRecoveryAppliesToTurn(recovery, null) ? [] : queued).bubbles).toHaveLength(1);
	});
});

describe('restingOnNotSentRows', () => {
	const prompt: ChatMessage = { id: 'p', role: 'user', type: 'text', content: 'next prompt' };

	it('is the chat ending on refused words', () => {
		expect(restingOnNotSentRows([reply, row('1')])).toBe(true);
		expect(restingOnNotSentRows([row('1'), reply])).toBe(true);
	});

	it('ends once a later prompt is the newest', () => {
		expect(restingOnNotSentRows([row('1'), prompt, reply])).toBe(false);
	});
});

describe('createNotSentRowsArrival', () => {
	const prompt: ChatMessage = { id: 'p', role: 'user', type: 'text', content: 'prompt' };
	const ended = [prompt, reply, row('1', 'live')];

	it('fires once, on the update that hands the rows over', () => {
		const arrival = createNotSentRowsArrival();
		const during = holdNotSentRows(ended, 'live').messages;
		expect(arrival('s', during)).toBe(false);
		expect(arrival('s', during)).toBe(false);
		expect(arrival('s', holdNotSentRows(ended, null).messages)).toBe(true);
		expect(arrival('s', ended)).toBe(false);
	});

	it('does not fire when a chat opens resting on them', () => {
		const arrival = createNotSentRowsArrival();
		expect(arrival('s', [])).toBe(false);
		expect(arrival('s', ended)).toBe(false);
		const switched = createNotSentRowsArrival();
		expect(switched('other', [prompt])).toBe(false);
		expect(switched('s', ended)).toBe(false);
	});

	it('fires again for the next Stop', () => {
		const arrival = createNotSentRowsArrival();
		arrival('s', [prompt]);
		expect(arrival('s', ended)).toBe(true);
		const next: ChatMessage = { ...prompt, id: 'p2' };
		expect(arrival('s', [...ended, next])).toBe(false);
		expect(arrival('s', [...ended, next, row('2')])).toBe(true);
	});
});

describe('withHeldBubbles', () => {
	it('returns the queue itself when nothing is held', () => {
		const queued = [{ id: 'q', content: 'waiting', queuedAt: 1 }];
		expect(withHeldBubbles([], queued)).toBe(queued);
	});

	it('puts held bubbles first and never shows one twice', () => {
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		const queued = [
			{ id: 'turn-1', content: 'words 1', queuedAt: 1 },
			{ id: 'q', content: 'waiting', queuedAt: 2 },
		];
		expect(withHeldBubbles(held, queued).map(m => [m.id, m.status])).toEqual([
			['turn-1', 'stopped'],
			['q', undefined],
		]);
	});
});

describe('createStopBubbleHold', () => {
	const waiting = { id: 'turn-1', content: 'words 1', queuedAt: 1 };
	const withdrawing = { id: 'turn-2', content: 'words 2', queuedAt: 2, status: 'cancelling' as const };
	const transcript: ChatMessage[] = [reply];
	const pressed = (hold: ReturnType<typeof createStopBubbleHold>, bubbles: Parameters<ReturnType<typeof createStopBubbleHold>>[1], messages = transcript) =>
		hold('s', bubbles, true, messages);

	it('keeps a bubble the queue snapshot took away until its row is held', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false, transcript);
		expect(pressed(hold, []).map(m => [m.id, m.status])).toEqual([['turn-1', 'stopped']]);
		expect(pressed(hold, []).map(m => m.id)).toEqual(['turn-1']);
		const rows = [row('1', 'live')];
		const held = holdNotSentRows(rows, 'live').bubbles;
		expect(pressed(hold, held, rows)).toEqual(held);
	});

	it('matches a send the server has not named by its words', () => {
		const hold = createStopBubbleHold();
		hold('s', [{ id: 'cmd', clientMessageId: 'cmd', content: 'words 1', queuedAt: 1 }], false, transcript);
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		expect(pressed(hold, held).map(m => m.id)).toEqual(['turn-1']);
	});

	it('does not keep identical words under a different server id', () => {
		const hold = createStopBubbleHold();
		hold('s', [{ ...waiting, id: 'turn-9' }, waiting], false, transcript);
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		expect(pressed(hold, held).map(m => m.id)).toEqual(['turn-1', 'turn-9']);
	});

	it('does not carry a bubble into another chat', () => {
		const hold = createStopBubbleHold();
		hold('other chat', [waiting], false, transcript);
		expect(pressed(hold, [])).toEqual([]);
	});

	it('lets a message the person withdrew go', () => {
		const hold = createStopBubbleHold();
		hold('s', [withdrawing], false, transcript);
		expect(pressed(hold, [])).toEqual([]);
	});

	it('lets go of a message withdrawn after Stop', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false, transcript);
		expect(pressed(hold, [{ ...waiting, status: 'cancelling' }])).toEqual([{ ...waiting, status: 'cancelling' }]);
		expect(pressed(hold, [])).toEqual([]);
	});

	it('keeps nothing without a Stop, or once the turn ends', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false, transcript);
		expect(hold('s', [], false, transcript)).toEqual([]);
		hold('s', [waiting], false, transcript);
		expect(pressed(hold, [])).toHaveLength(1);
		expect(hold('s', [], false, transcript)).toEqual([]);
	});

	it('never shows one id twice', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false, transcript);
		pressed(hold, []);
		expect(pressed(hold, [waiting])).toEqual([waiting]);
	});

	it('never labels a message sent after Stop', () => {
		const fresh = { id: 'cmd-new', clientMessageId: 'cmd-new', content: 'after stop', queuedAt: 3 };
		const hold = createStopBubbleHold();
		hold('s', [waiting], false, transcript);
		expect(pressed(hold, [waiting, fresh])).toEqual([waiting, fresh]);
		// The server names it, then takes it to run: it is never "not sent".
		const named = { ...fresh, id: 'turn-new' };
		expect(pressed(hold, [waiting, named])).toEqual([waiting, named]);
		expect(pressed(hold, [waiting]).some(m => m.status === 'stopped')).toBe(false);
		expect(pressed(hold, []).map(m => m.id)).toEqual(['turn-1']);
	});

	it('stops keeping a message the transcript shows delivered', () => {
		const hold = createStopBubbleHold();
		const named = { ...waiting, clientMessageId: 'cmd-1' };
		const other = { id: 'turn-2', clientMessageId: 'cmd-2', content: 'words 2', queuedAt: 2 };
		hold('s', [named, other], false, transcript);
		// By the turn the queue named it, the way a steered message lands...
		const steered: ChatMessage = { id: 'm1', role: 'user', type: 'text', content: 'words 1', turnId: 'turn-1' };
		expect(pressed(hold, [other], [reply, steered])).toEqual([other]);
		// ...and by the command that sent it. Neither comes back with the queue empty.
		const byCommand: ChatMessage = { id: 'm2', role: 'user', type: 'text', content: 'words 2', commandId: 'cmd-2' };
		expect(pressed(hold, [], [reply, steered, byCommand])).toEqual([]);
		expect(pressed(hold, [], [reply, steered, byCommand])).toEqual([]);
	});
});
