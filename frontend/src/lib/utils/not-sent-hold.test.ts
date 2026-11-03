import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { createNotSentHold, createNotSentRowsArrival, createStopBubbleHold, holdNotSentRows, restingOnNotSentRows, withHeldBubbles } from './not-sent-hold';

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
	it('keeps the same transcript array until its input changes', () => {
		const hold = createNotSentHold();
		const messages = [reply, row('1', 'live')];
		const first = hold(messages, 'live');
		expect(hold(messages, 'live').messages).toBe(first.messages);
		expect(hold(messages, null).messages).toBe(messages);
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
