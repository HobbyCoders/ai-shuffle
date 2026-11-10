import { expect, it } from 'vitest';

import { chatRunsElsewhere } from './runs-elsewhere';

const open = { sessionId: 's1', deviceIntent: null };

it('reads an open chat\'s owner from its row first, then from /live', () => {
	expect(chatRunsElsewhere({ ...open, isRemote: false }, { is_remote: true })).toBe(true);
	expect(chatRunsElsewhere({ ...open, isRemote: true }, { is_remote: false })).toBe(false);
	expect(chatRunsElsewhere({ ...open, isRemote: true }, null)).toBe(true);
	expect(chatRunsElsewhere({ ...open, isRemote: false }, undefined)).toBe(false);
});

it('treats an open chat whose owner is not known yet as elsewhere', () => {
	expect(chatRunsElsewhere({ ...open, isRemote: undefined }, null)).toBe(true);
	expect(chatRunsElsewhere({ ...open, isRemote: undefined }, { is_remote: null })).toBe(true);
});

it('follows the "Runs on" pick for a new chat', () => {
	expect(chatRunsElsewhere({ sessionId: null, isRemote: undefined, deviceIntent: null })).toBe(false);
	expect(chatRunsElsewhere({ sessionId: null, isRemote: undefined, deviceIntent: { id: 'dev_b', name: 'B' } })).toBe(true);
	// A stale owner from the previous chat does not count for a new one.
	expect(chatRunsElsewhere({ sessionId: null, isRemote: true, deviceIntent: null })).toBe(false);
});

it('counts a message held for a queued move as elsewhere', () => {
	// The move sends it to the new worker, whatever the row says now.
	expect(chatRunsElsewhere({ ...open, isRemote: false }, { is_remote: false }, true)).toBe(true);
	expect(chatRunsElsewhere({ ...open, isRemote: false }, { is_remote: false }, false)).toBe(false);
});

it('counts an open chat as elsewhere until the session list answers a "sessions" signal', () => {
	const local = { ...open, isRemote: false };
	const row = { is_remote: false };
	// A signal (a hand-off, a reconnect) the list has not answered yet.
	expect(chatRunsElsewhere(local, row, false, { signals: 2, answered: 1 })).toBe(true);
	// Answered: the row is the list fetched after the signal.
	expect(chatRunsElsewhere(local, row, false, { signals: 2, answered: 2 })).toBe(false);
	// Answered, but this chat has no row there: /live may predate the signal.
	expect(chatRunsElsewhere(local, null, false, { signals: 2, answered: 2 })).toBe(true);
	// No signal since the page loaded: /live's answer still stands.
	expect(chatRunsElsewhere(local, null, false, { signals: 0, answered: 0 })).toBe(false);
});
