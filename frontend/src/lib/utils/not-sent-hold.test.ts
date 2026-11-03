import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { createNotSentHold, createStopBubbleHold, holdNotSentRows, restingOnNotSentRows, withHeldBubbles } from './not-sent-hold';

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

	it('holds the reader only between the hand-over and the settle pin', () => {
		const messages = [reply, row('1', 'live')];
		expect(restingOnNotSentRows(messages, 'live', true)).toBe(false);
		expect(restingOnNotSentRows(messages, null, true)).toBe(true);
		expect(restingOnNotSentRows(messages, null, false)).toBe(false);
	});

	it('lets a reader follow again once a later prompt is the newest', () => {
		expect(restingOnNotSentRows([row('1'), prompt, reply], null, true)).toBe(false);
	});

	it('still holds when the [Stopped] reply lands after the rows', () => {
		expect(restingOnNotSentRows([row('1'), reply], null, true)).toBe(true);
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

	it('keeps a bubble the queue snapshot took away until its row is held', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false);
		expect(hold('s', [], true).map(m => [m.id, m.status])).toEqual([['turn-1', 'stopped']]);
		expect(hold('s', [], true).map(m => m.id)).toEqual(['turn-1']);
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		expect(hold('s', held, true)).toEqual(held);
	});

	it('matches a send the server has not named by its words', () => {
		const hold = createStopBubbleHold();
		hold('s', [{ id: 'cmd', clientMessageId: 'cmd', content: 'words 1', queuedAt: 1 }], false);
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		expect(hold('s', held, true).map(m => m.id)).toEqual(['turn-1']);
	});

	it('does not keep identical words under a different server id', () => {
		const hold = createStopBubbleHold();
		hold('s', [{ ...waiting, id: 'turn-9' }, waiting], false);
		const held = holdNotSentRows([row('1', 'live')], 'live').bubbles;
		expect(hold('s', held, true).map(m => m.id)).toEqual(['turn-1', 'turn-9']);
	});

	it('does not carry a bubble into another chat', () => {
		const hold = createStopBubbleHold();
		hold('other chat', [waiting], false);
		expect(hold('s', [], true)).toEqual([]);
	});

	it('lets a message the person withdrew go', () => {
		const hold = createStopBubbleHold();
		hold('s', [withdrawing], false);
		expect(hold('s', [], true)).toEqual([]);
	});

	it('keeps nothing without a Stop, or once the turn ends', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], false);
		expect(hold('s', [], false)).toEqual([]);
		hold('s', [waiting], true);
		expect(hold('s', [], true)).toHaveLength(1);
		expect(hold('s', [], false)).toEqual([]);
	});

	it('never shows one id twice', () => {
		const hold = createStopBubbleHold();
		hold('s', [waiting], true);
		hold('s', [], true);
		expect(hold('s', [waiting], true)).toEqual([waiting]);
	});
});
