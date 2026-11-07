import { describe, expect, it } from 'vitest';
import {
	followTurnError,
	raiseTurnError,
	unplaceTurnError,
	type TurnErrorAnchor,
} from './turn-error-banner';
import { reduce, reduceAll, TURN_FAILURES_KEPT, type V2SessionState, type WireEvent } from './v2/reducer';

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
const abort = (turn: string, seq: number, message = FAILED) =>
	ev('turn.aborted', seq, { turn_id: turn, reason: 'error', message });
const others = (from: number, count: number) =>
	Array.from({ length: count }, (_, i) => failed(`o${i}`, from + i * 3, `other failure ${i}`)).flat();

type Banner = 'shown' | 'cleared';

/**
 * One chat as the browser holds it, driven the way the feed controller drives
 * it: each event is reduced and steps the banner's anchor (`onEvent`), the
 * banner changes only at a fold (`fold`). Every intermediate fold of a
 * replay is checked.
 */
class Chat {
	state: V2SessionState;
	error: string | null = null;
	anchor: TurnErrorAnchor | null = null;

	constructor(log: WireEvent[] = []) {
		this.state = reduceAll(log);
	}

	get banner(): Banner {
		return this.error === null ? 'cleared' : 'shown';
	}

	/** The socket's `error` frame: what the chat shows right after it. */
	raise(turnId: string | null = null, message = FAILED): Banner | 'never raised' {
		const anchor = raiseTurnError(message, turnId, this.state.turnOutcomes);
		if (anchor === 'outlived') return 'never raised';
		this.error = message;
		this.anchor = anchor;
		return this.banner;
	}

	/** Reduce one batch event by event, then fold. */
	fold(batch: WireEvent[]): Banner {
		for (const e of batch) {
			this.state = reduce(this.state, e);
			if (this.anchor) this.anchor = followTurnError(this.anchor, e);
		}
		if (this.anchor?.outlived && this.error === this.anchor.message) this.error = null;
		if (this.anchor && (this.anchor.outlived || this.error !== this.anchor.message)) this.anchor = null;
		return this.banner;
	}

	/** Fold each batch in turn; the banner after every one. */
	replay(batches: WireEvent[][]): Banner[] {
		return batches.map((b) => this.fold(b));
	}

	/** The server's `caught_up` sentinel: hydration's batch is shown. */
	sentinel(): Banner {
		return this.fold([]);
	}
}

describe('turn-error banner, tied to its turn by the frame', () => {
	it('keeps the banner while the failed turn is still the latest outcome', () => {
		const chat = new Chat(succeeded('t0', 1));
		expect(chat.raise('t1')).toBe('shown');
		expect(chat.replay([prompt('t1', 4), [abort('t1', 6)], []])).toEqual(['shown', 'shown', 'shown']);
	});

	it('clears it once a different turn finishes cleanly after it (#1175)', () => {
		const chat = new Chat(prompt('t1', 1));
		chat.raise('t1');
		expect(chat.replay([[abort('t1', 3)], prompt('t2', 4), [ev('turn.completed', 6, { turn_id: 't2' })]]))
			.toEqual(['shown', 'shown', 'cleared']);
	});

	it('does not count a failed completion (429, is_error, errors) as a clean finish', () => {
		for (const [extra, after] of [
			[{ api_error_status: 429 }, 'shown'],
			[{ is_error: true }, 'shown'],
			[{ errors: ['overloaded'] }, 'shown'],
			[{ errors: [] }, 'cleared'],
		] as const) {
			const chat = new Chat(failed('t1', 1));
			chat.raise('t1');
			expect(chat.fold(succeeded('t2', 4, extra)), JSON.stringify(extra)).toBe(after);
		}
	});

	it('follows a turn that ended in a failed completion, already reduced or not', () => {
		const live = new Chat(prompt('t1', 1));
		live.raise('t1');
		expect(live.replay([[ev('turn.completed', 3, { turn_id: 't1', api_error_status: 429 })], succeeded('t2', 4)]))
			.toEqual(['shown', 'cleared']);

		const late = new Chat([...prompt('t1', 1), ev('turn.completed', 3, { turn_id: 't1', is_error: true })]);
		late.raise('t1');
		expect(late.anchor?.failure).toEqual({ seq: 3, turnId: 't1' });
		expect(late.fold(succeeded('t2', 4))).toBe('cleared');
	});

	it('is not cleared when the failed turn itself later reports completed', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise('t1');
		expect(chat.fold([ev('turn.completed', 4, { turn_id: 't1' })])).toBe('shown');
	});

	it('clears it once a rewind cuts the failed turn away (#1082), not when the cut is after it', () => {
		const cut = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		cut.raise('t1');
		expect(cut.fold([ev('transcript.rewound', 7, { from_seq: 5 })])).toBe('cleared');

		const after = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		after.raise('t1');
		expect(after.fold([...succeeded('t2', 7, { api_error_status: 429 }), ev('transcript.rewound', 10, { from_seq: 8 })])).toBe('shown');
	});

	it('a frame naming an outlived turn raises nothing', () => {
		const chat = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		expect(chat.raise('t1')).toBe('never raised');
	});

	it('only its own turn: a same-words failure of another turn is not it', () => {
		const chat = new Chat([...failed('t1', 1), ...failed('t2', 4)]);
		chat.raise('t1');
		expect(chat.fold([ev('transcript.rewound', 7, { from_seq: 5 })])).toBe('shown');
		expect(chat.fold([ev('transcript.rewound', 8, { from_seq: 2 })])).toBe('cleared');
	});

	it('leaves any other banner alone, and a frame with no message', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise('t1');
		chat.error = 'This session was taken over by another device.';
		expect(chat.fold(succeeded('t2', 4))).toBe('shown');
		expect(chat.anchor).toBeNull();
		expect(raiseTurnError('', 't1')).toBeNull();
	});
});

describe('turn-error banner while the feed lags or replays (review S2)', () => {
	// The log: an old failure with the same words, a success, then the failure
	// the frame is about, then its successor.
	const oldFailure = failed('t0', 1); // abort at 3
	const success = succeeded('tx', 4); // completed at 6
	const current = failed('t1', 7); // abort at 9
	const successor = succeeded('t2', 10); // completed at 12

	it('(a) a caught-up feed sitting at an old same-words failure: the frame binds to its own turn', () => {
		const chat = new Chat(oldFailure);
		expect(chat.raise('t1')).toBe('shown');
		expect(chat.replay([success, current, successor])).toEqual(['shown', 'shown', 'cleared']);
	});

	it('(b) a timely frame during hydration, then its failure and a clean successor: cleared at the sentinel', () => {
		const chat = new Chat();
		expect(chat.raise('t1')).toBe('shown');
		// Hydration folds nothing on screen before its sentinel, but whatever
		// batches it is cut into, no point before the successor clears it.
		expect(chat.replay([oldFailure, success, current])).toEqual(['shown', 'shown', 'shown']);
		expect(chat.replay([successor])).toEqual(['cleared']);
		expect(chat.sentinel()).toBe('cleared');

		const atomic = new Chat();
		atomic.raise('t1');
		atomic.fold([...oldFailure, ...success, ...current, ...successor]);
		expect(atomic.sentinel()).toBe('cleared');
	});

	it('a turn-named banner follows a reconnect gap fold by fold', () => {
		const named = new Chat(prompt('t1', 1));
		named.raise('t1');
		expect(named.replay([[abort('t1', 3)], succeeded('t2', 4)])).toEqual(['shown', 'cleared']);
	});

	it('a restarted feed (replaced log) finds the turn-named banner\'s failure again', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise('t1');
		chat.anchor = unplaceTurnError(chat.anchor!);
		chat.state = reduceAll([]);
		expect(chat.replay([failed('t1', 1), []])).toEqual(['shown', 'shown']);
		expect(chat.fold(succeeded('t2', 4))).toBe('cleared');
	});
});

describe('turn-error banner past the recent-failure list (review S6)', () => {
	it('raised during hydration, then one atomic replay of its failure, 16 others, a clean finish and more', () => {
		const chat = new Chat();
		chat.raise('t1');
		const replay = [...failed('t1', 1), ...others(4, 16), ...succeeded('t2', 100), ...failed('t3', 103, 'later')];
		chat.fold(replay);
		expect(chat.sentinel()).toBe('cleared');
	});

	it('the same replay stops short of the clean finish: still shown', () => {
		const chat = new Chat();
		chat.raise('t1');
		chat.fold([...failed('t1', 1), ...others(4, 17)]);
		expect(chat.sentinel()).toBe('shown');
		expect(chat.fold(succeeded('t2', 100))).toBe('cleared');
	});

	it('after binding: a rewind still clears it after eviction when it cuts the failure, not when it cuts later', () => {
		const cut = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		cut.raise('t1');
		cut.fold(others(7, 16));
		expect(cut.fold([ev('transcript.rewound', 100, { from_seq: 4 })])).toBe('cleared');

		const later = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		later.raise('t1');
		later.fold(others(7, 16));
		expect(later.fold([ev('transcript.rewound', 100, { from_seq: 8 })])).toBe('shown');
		expect(later.fold([ev('transcript.rewound', 101, { from_seq: 2 })])).toBe('cleared');
	});

	it('the failed turn reporting completed is still no clean finish after eviction', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise('t1');
		chat.fold(others(4, 16));
		expect(chat.fold([ev('turn.completed', 100, { turn_id: 't1' })])).toBe('shown');
		expect(chat.fold(succeeded('t2', 101))).toBe('cleared');
	});
});

describe('turn-error banner from a frame naming no turn (older workers, review round 4)', () => {
	it('whole-turn lag: an older same-words failure and its clean successor do not clear the current one', () => {
		// The feed is caught up but still at seq 0 when the current failure's frame
		// arrives; then it delivers an older failure with the same words, its clean
		// successor, and only then the current failure.
		const chat = new Chat();
		expect(chat.raise(null)).toBe('shown');
		expect(chat.raise(null) && chat.anchor).toBeNull();
		const log = [...failed('t0', 1), ...succeeded('tx', 4), ...failed('t1', 7)];
		expect(chat.replay(log.map((e) => [e]))).toEqual(log.map(() => 'shown'));
		expect(chat.error).toBe(FAILED);
	});

	it('nothing in the log retires it, in any batching; sending or dismissing does', () => {
		const log = [...failed('t0', 1), ...succeeded('tx', 4), ...failed('t1', 7), ...succeeded('t2', 10),
			ev('transcript.rewound', 13, { from_seq: 1 })];
		for (const batches of [[log], log.map((e) => [e])]) {
			const chat = new Chat();
			chat.raise(null);
			expect(chat.replay(batches).every((b) => b === 'shown')).toBe(true);
		}
		// A frame with no turn replaces a turn-named anchor: the new words have nothing to follow.
		const chat = new Chat(prompt('t1', 1));
		chat.raise('t1');
		chat.raise(null, 'connection lost');
		expect(chat.fold([abort('t1', 3), ...succeeded('t2', 4)])).toBe('shown');
	});
});

describe('turn-error banner from a late frame past many failures (review round 4, S6)', () => {
	it('hydrated: t0 failed, then 16 more failures and a clean finish; t0\'s delayed frame raises nothing', () => {
		const chat = new Chat([...failed('t0', 1), ...others(4, 16), ...succeeded('t2', 100)]);
		expect(chat.raise('t0')).toBe('never raised');
		expect(chat.error).toBeNull();
	});

	it('the same with no clean finish yet: it raises, and the next clean finish clears it', () => {
		const chat = new Chat([...failed('t0', 1), ...others(4, 16)]);
		expect(chat.raise('t0')).toBe('shown');
		expect(chat.anchor?.failure).toEqual({ seq: 3, turnId: 't0' });
		expect(chat.fold(succeeded('t2', 100))).toBe('cleared');
	});

	it('the failure index keeps far more than 16 turns, one entry per turn', () => {
		const state = reduceAll([...failed('t0', 1), ev('turn.completed', 4, { turn_id: 't0', is_error: true }), ...others(5, 40)]);
		const failures = state.turnOutcomes?.failures ?? [];
		expect(failures).toHaveLength(41);
		expect(failures[0]).toEqual({ turnId: 't0', seq: 3 });
		expect(TURN_FAILURES_KEPT).toBeGreaterThanOrEqual(1024);
	});
});
