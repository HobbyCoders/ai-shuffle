import { describe, expect, it } from 'vitest';
import { turnErrorAnchor, turnErrorOutlived } from './turn-error-banner';
import { reduceAll, type WireEvent } from './v2/reducer';

const ev = (type: string, seq: number, fields: Record<string, unknown> = {}): WireEvent =>
	({ type, id: `e${seq}`, seq, session_id: 's1', created_at: '2026-11-06T00:00:00.000Z', ...fields }) as WireEvent;

const FAILED = 'provider returned HTTP 500';

describe('turnErrorOutlived', () => {
	const anchor = turnErrorAnchor(FAILED, 3, 't1');

	it('keeps the banner while the failed turn is still the latest outcome', () => {
		expect(turnErrorOutlived(anchor, FAILED, {})).toBe(false);
		expect(turnErrorOutlived(anchor, FAILED, { lastCompletedTurn: { id: 't0', seq: 2 }, lastRewindSeq: 1 })).toBe(false);
	});

	it('clears it once a different turn finished after it (#1175)', () => {
		expect(turnErrorOutlived(anchor, FAILED, { lastCompletedTurn: { id: 't2', seq: 9 } })).toBe(true);
	});

	it('does not count the failed turn finishing as a later success', () => {
		expect(turnErrorOutlived(anchor, FAILED, { lastCompletedTurn: { id: 't1', seq: 5 } })).toBe(false);
	});

	it('clears it once a rewind after it cut the turn away (#1082)', () => {
		expect(turnErrorOutlived(anchor, FAILED, { lastRewindSeq: 6 })).toBe(true);
	});

	it('leaves any other banner alone, and a banner with no anchor', () => {
		const later = { lastCompletedTurn: { id: 't2', seq: 9 }, lastRewindSeq: 9 };
		expect(turnErrorOutlived(anchor, 'This session was taken over by another device.', later)).toBe(false);
		expect(turnErrorOutlived(anchor, null, later)).toBe(false);
		expect(turnErrorOutlived(null, FAILED, later)).toBe(false);
	});

	it('takes no anchor when the feed position is unknown', () => {
		expect(turnErrorAnchor(FAILED, null, 't1')).toBeNull();
	});
});

describe('reducer facts the banner rule reads', () => {
	it('records the latest clean turn finish and the latest rewind', () => {
		const s = reduceAll([
			ev('turn.started', 1, { turn_id: 't1' }),
			ev('message.user', 2, { turn_id: 't1', content: 'hi' }),
			ev('turn.completed', 3, { turn_id: 't1' }),
			ev('turn.started', 4, { turn_id: 't2' }),
			ev('turn.aborted', 5, { turn_id: 't2', reason: 'error', message: FAILED }),
		]);
		expect(s.lastCompletedTurn).toEqual({ id: 't1', seq: 3 });
		expect(s.lastRewindSeq ?? 0).toBe(0);
		const rewound = reduceAll([ev('transcript.rewound', 6, { from_seq: 4 })], s);
		expect(rewound.lastRewindSeq).toBe(6);
	});
});
