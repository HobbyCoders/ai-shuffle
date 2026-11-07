import { describe, expect, it } from 'vitest';
import { turnErrorAnchor, turnErrorOutlived, type TurnErrorAnchor } from './turn-error-banner';
import { reduceAll, type V2SessionState, type WireEvent } from './v2/reducer';

const ev = (type: string, seq: number, fields: Record<string, unknown> = {}): WireEvent =>
	({ type, id: `e${seq}`, seq, session_id: 's1', created_at: '2026-11-06T00:00:00.000Z', ...fields }) as WireEvent;

const FAILED = 'provider returned HTTP 500';

const prompt = (turn: string, seq: number) => [
	ev('turn.started', seq, { turn_id: turn }),
	ev('message.user', seq + 1, { turn_id: turn, content: `prompt ${turn}` }),
];
const succeeded = (turn: string, seq: number, extra: Record<string, unknown> = {}) =>
	[...prompt(turn, seq), ev('turn.completed', seq + 2, { turn_id: turn, ...extra })];
const failed = (turn: string, seq: number, message = FAILED) =>
	[...prompt(turn, seq), ev('turn.aborted', seq + 2, { turn_id: turn, reason: 'error', message })];

/** The banner as the socket raises it at `at`, then the feed reduced to `later`. */
function bannerAfter(at: V2SessionState, later: WireEvent[], running = false, turnId: string | null = null) {
	const anchor = turnErrorAnchor(FAILED, turnId, at, running);
	if (anchor === 'outlived') return 'never raised';
	const end = reduceAll(later, at);
	return turnErrorOutlived(anchor, FAILED, end) ? 'cleared' : 'shown';
}

describe('turn-error banner, read from the log', () => {
	it('keeps the banner while the failed turn is still the latest outcome', () => {
		const s = reduceAll([...succeeded('t0', 1), ...failed('t1', 4)]);
		expect(bannerAfter(s, [])).toBe('shown');
	});

	it('clears it once a different turn finishes cleanly after it (#1175)', () => {
		const s = reduceAll(failed('t1', 1));
		expect(bannerAfter(s, succeeded('t2', 4))).toBe('cleared');
	});

	it('does not count a failed completion (429, is_error, errors) as a clean finish', () => {
		const s = reduceAll(failed('t1', 1));
		expect(bannerAfter(s, succeeded('t2', 4, { api_error_status: 429 }))).toBe('shown');
		expect(bannerAfter(s, succeeded('t2', 4, { is_error: true }))).toBe('shown');
		expect(bannerAfter(s, succeeded('t2', 4, { errors: ['overloaded'] }))).toBe('shown');
		expect(bannerAfter(s, succeeded('t2', 4, { errors: [] }))).toBe('cleared');
	});

	it('clears it once a rewind cuts the failed turn away (#1082), not when the cut is after it', () => {
		const s = reduceAll([...succeeded('t0', 1), ...failed('t1', 4)]);
		expect(bannerAfter(s, [ev('transcript.rewound', 7, { from_seq: 5 })])).toBe('cleared');
		expect(bannerAfter(s, [...succeeded('t2', 7, { api_error_status: 429 }), ev('transcript.rewound', 10, { from_seq: 8 })])).toBe('shown');
	});

	it('waits for its failure when the frame beats the feed, then follows that failure', () => {
		const before = reduceAll(prompt('t1', 1));
		expect(bannerAfter(before, [ev('turn.aborted', 3, { turn_id: 't1', reason: 'error', message: FAILED })], true)).toBe('shown');
		expect(bannerAfter(before, [ev('turn.aborted', 3, { turn_id: 't1', reason: 'error', message: FAILED }), ...succeeded('t2', 4)], true)).toBe('cleared');
	});

	it('is not cleared by an old success replayed after the frame (reload / resume)', () => {
		// The frame arrives with the feed at seq 0; the replay then brings an old
		// success and the current failure, in log order.
		const empty = reduceAll([]);
		expect(bannerAfter(empty, [...succeeded('t0', 1), ...failed('t1', 5)])).toBe('shown');
		// An old failure with the same words, long since outlived, is not this one.
		expect(bannerAfter(empty, [...failed('t0', 1), ...succeeded('tx', 4), ...failed('t1', 8)])).toBe('shown');
	});

	it('raises nothing for a frame whose failure the log has already moved past', () => {
		const s = reduceAll([...failed('t1', 1), ...succeeded('t2', 4)]);
		expect(bannerAfter(s, [])).toBe('never raised');
		expect(bannerAfter(s, [], true, 't1')).toBe('never raised');
	});

	it('when a new run fails with the same words before its abort arrives, waits for that abort', () => {
		const s = reduceAll([...failed('t1', 1), ...succeeded('t2', 4), ...prompt('t3', 7)]);
		expect(bannerAfter(s, [], true)).toBe('shown');
		expect(bannerAfter(s, [ev('turn.aborted', 9, { turn_id: 't3', reason: 'error', message: FAILED })], true)).toBe('shown');
		expect(bannerAfter(s, [ev('turn.aborted', 9, { turn_id: 't3', reason: 'error', message: FAILED }), ...succeeded('t4', 10)], true)).toBe('cleared');
	});

	it('matches by the frame\'s turn id when it carries one', () => {
		const s = reduceAll([...failed('t1', 1, 'first words'), ...failed('t2', 4, 'other words')]);
		const anchor = turnErrorAnchor(FAILED, 't2', s, false) as TurnErrorAnchor;
		expect(turnErrorOutlived(anchor, FAILED, reduceAll([ev('transcript.rewound', 7, { from_seq: 5 })], s))).toBe(true);
	});

	it('leaves any other banner alone, and a banner with no anchor', () => {
		const s = reduceAll(failed('t1', 1));
		const anchor = turnErrorAnchor(FAILED, null, s, false) as TurnErrorAnchor;
		const later = reduceAll(succeeded('t2', 4), s);
		expect(turnErrorOutlived(anchor, 'This session was taken over by another device.', later)).toBe(false);
		expect(turnErrorOutlived(anchor, null, later)).toBe(false);
		expect(turnErrorOutlived(null, FAILED, later)).toBe(false);
		expect(turnErrorAnchor('', null, s, false)).toBeNull();
	});
});
