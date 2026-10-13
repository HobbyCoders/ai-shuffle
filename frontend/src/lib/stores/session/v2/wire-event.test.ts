import { describe, expect, it } from 'vitest';
import { parseWireEvent } from './reducer';
import type { TurnStarted } from '$lib/contract/events';

describe('session event envelope', () => {
	it('accepts generated event shapes', () => {
		const event = { type: 'turn.started', id: 'event', seq: 1, session_id: 'session', created_at: '2026-09-04T00:00:00Z' } satisfies TurnStarted;
		expect(parseWireEvent(event)).toBe(event);
	});
	it('retains unknown future events and extension fields', () => {
		const event = { type: 'future.extension', id: 'event', seq: 3, extension: { data: true } };
		expect(parseWireEvent(event)).toBe(event);
	});
	it.each([null, {}, { type: 'turn.started', id: 4, seq: 1 },
		{ type: 'turn.started', id: 'event', seq: NaN },
		{ type: 'turn.started', id: 'event', seq: -1 },
		{ type: 'turn.started', id: 'event', seq: 1, turn_id: 42 }])('rejects malformed envelope %j', (value) => {
		expect(parseWireEvent(value)).toBeNull();
	});
});
