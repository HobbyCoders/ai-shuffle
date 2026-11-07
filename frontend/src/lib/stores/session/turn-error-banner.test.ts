import { describe, expect, it } from 'vitest';
import {
	followTurnError,
	placeTurnError,
	raiseTurnError,
	unplaceTurnError,
	type TurnErrorAnchor,
} from './turn-error-banner';
import { reduce, reduceAll, type V2SessionState, type WireEvent } from './v2/reducer';

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
 * banner changes only at a fold (`fold`), and a `caught_up` sentinel places a
 * message-only anchor. Every intermediate fold of a replay is checked.
 */
class Chat {
	state: V2SessionState;
	caughtUp: boolean;
	error: string | null = null;
	anchor: TurnErrorAnchor | null = null;

	constructor(log: WireEvent[] = [], caughtUp = true) {
		this.state = reduceAll(log);
		this.caughtUp = caughtUp;
	}

	private facts() {
		return { outcomes: this.state.turnOutcomes, lastSeq: this.state.lastSeq, caughtUp: this.caughtUp };
	}

	get banner(): Banner {
		return this.error === null ? 'cleared' : 'shown';
	}

	/** The socket's `error` frame: what the chat shows right after it. */
	raise(turnId: string | null = null, message = FAILED): Banner | 'never raised' {
		const anchor = raiseTurnError(message, turnId, this.facts());
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

	/** The server's `caught_up` sentinel: the reduced log is complete. */
	sentinel(): Banner {
		this.caughtUp = true;
		if (this.anchor) this.anchor = placeTurnError(this.anchor, this.facts());
		return this.fold([]);
	}

	/** A reconnect: its gap replays until the next sentinel. */
	reconnect(): void {
		this.caughtUp = false;
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

	it('a frame naming an outlived turn raises nothing; an outlived one by its words alone still shows', () => {
		const chat = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		expect(chat.raise('t1')).toBe('never raised');
		expect(chat.raise(null)).toBe('shown');
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
		expect(raiseTurnError('', 't1', { caughtUp: true })).toBeNull();
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

	it('(a) the same with a message-only frame: nothing before a same-words failure logged after it counts', () => {
		const chat = new Chat(oldFailure);
		expect(chat.raise(null)).toBe('shown');
		expect(chat.replay([success, current, successor])).toEqual(['shown', 'shown', 'cleared']);
		// Event by event too.
		const perEvent = new Chat(oldFailure);
		perEvent.raise(null);
		const log = [...success, ...current, ...successor];
		expect(perEvent.replay(log.map((e) => [e])).slice(0, -1)).toEqual(log.slice(0, -1).map(() => 'shown'));
		expect(perEvent.banner).toBe('cleared');
	});

	it('(b) a timely frame during hydration, then its failure and a clean successor: cleared at the sentinel', () => {
		const chat = new Chat([], false);
		expect(chat.raise('t1')).toBe('shown');
		// Hydration folds nothing on screen before its sentinel, but whatever
		// batches it is cut into, no point before the successor clears it.
		expect(chat.replay([oldFailure, success, current])).toEqual(['shown', 'shown', 'shown']);
		expect(chat.replay([successor])).toEqual(['cleared']);
		expect(chat.sentinel()).toBe('cleared');

		const atomic = new Chat([], false);
		atomic.raise('t1');
		atomic.fold([...oldFailure, ...success, ...current, ...successor]);
		expect(atomic.sentinel()).toBe('cleared');
	});

	it('(b) the same frame by its words alone stays: it may have been about any of them (older worker)', () => {
		const chat = new Chat([], false);
		chat.raise(null);
		expect(chat.replay([oldFailure, success, current, successor])).toEqual(['shown', 'shown', 'shown', 'shown']);
		expect(chat.sentinel()).toBe('shown');
		// Placed at the sentinel: a same-words failure logged after it is its own.
		expect(chat.anchor?.afterSeq).toBe(12);
		expect(chat.replay([failed('t3', 13), succeeded('t4', 16)])).toEqual(['shown', 'cleared']);
	});

	it('a reconnect gap: a turn-named banner follows it fold by fold; a message-only one waits for its sentinel', () => {
		const named = new Chat(prompt('t1', 1));
		named.raise('t1');
		named.reconnect();
		expect(named.replay([[abort('t1', 3)], succeeded('t2', 4)])).toEqual(['shown', 'cleared']);

		// Raised mid-gap: the gap's same-words failure and success came before it.
		const words = new Chat(succeeded('t0', 1));
		words.reconnect();
		words.raise(null);
		expect(words.replay([failed('t1', 4), succeeded('t2', 7)])).toEqual(['shown', 'shown']);
		expect(words.sentinel()).toBe('shown');
		expect(words.replay([failed('t3', 10), succeeded('t4', 13)])).toEqual(['shown', 'cleared']);
	});

	it('a restarted feed (replaced log) finds the turn-named banner\'s failure again', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise('t1');
		chat.anchor = unplaceTurnError(chat.anchor!);
		chat.state = reduceAll([]);
		chat.caughtUp = false;
		expect(chat.replay([failed('t1', 1), []])).toEqual(['shown', 'shown']);
		expect(chat.fold(succeeded('t2', 4))).toBe('cleared');
	});
});

describe('turn-error banner past the recent-failure list (review S6)', () => {
	it('raised during hydration, then one atomic replay of its failure, 16 others, a clean finish and more', () => {
		const chat = new Chat([], false);
		chat.raise('t1');
		const replay = [...failed('t1', 1), ...others(4, 16), ...succeeded('t2', 100), ...failed('t3', 103, 'later')];
		chat.fold(replay);
		expect(chat.state.turnOutcomes?.failures.some((f) => f.turnId === 't1')).toBe(false);
		expect(chat.sentinel()).toBe('cleared');
	});

	it('the same replay stops short of the clean finish: still shown', () => {
		const chat = new Chat([], false);
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
