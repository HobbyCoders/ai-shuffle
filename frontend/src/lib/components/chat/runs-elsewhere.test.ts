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
