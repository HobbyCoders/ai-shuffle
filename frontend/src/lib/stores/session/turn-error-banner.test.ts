import { describe, expect, it } from 'vitest';
import { followTurnError, raiseTurnError, type TurnErrorAnchor } from './turn-error-banner';
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
const abort = (turn: string, seq: number, message = FAILED) =>
	ev('turn.aborted', seq, { turn_id: turn, reason: 'error', message });

type Banner = 'shown' | 'cleared' | 'never raised';

/**
 * One chat as the browser holds it: the feed's reduced log plus the banner,
 * folded batch by batch the way the feed controller folds (`fold`), so every
 * intermediate state a replay passes through is checked, not just the last.
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

	/** The socket's `error` frame. */
	raise(turnId: string | null = null, message = FAILED): Banner | 'never raised' {
		const anchor = raiseTurnError(message, turnId, this.facts());
		if (anchor === 'outlived') return 'never raised';
		this.error = message;
		this.anchor = anchor;
		return this.banner;
	}

	/** Reduce one batch, then fold: what the chat shows after it. */
	fold(batch: WireEvent[], caughtUp = this.caughtUp): Banner {
		this.state = reduceAll(batch, this.state);
		this.caughtUp = caughtUp;
		const next = followTurnError(this.anchor, this.error, this.facts());
		if (next === 'outlived') {
			this.error = null;
			this.anchor = null;
		} else {
			this.anchor = next;
		}
		return this.banner;
	}

	/** Fold each batch in turn; the banner after every one. */
	replay(batches: WireEvent[][], caughtUp = this.caughtUp): Banner[] {
		return batches.map((b) => this.fold(b, caughtUp));
	}
}

describe('turn-error banner, read from the log', () => {
	it('keeps the banner while the failed turn is still the latest outcome', () => {
		const chat = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		expect(chat.raise()).toBe('shown');
		expect(chat.fold([])).toBe('shown');
	});

	it('clears it once a different turn finishes cleanly after it (#1175)', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise();
		expect(chat.replay([prompt('t2', 4), [ev('turn.completed', 6, { turn_id: 't2' })]])).toEqual(['shown', 'cleared']);
	});

	it('does not count a failed completion (429, is_error, errors) as a clean finish', () => {
		for (const [extra, after] of [
			[{ api_error_status: 429 }, 'shown'],
			[{ is_error: true }, 'shown'],
			[{ errors: ['overloaded'] }, 'shown'],
			[{ errors: [] }, 'cleared'],
		] as const) {
			const chat = new Chat(failed('t1', 1));
			chat.raise();
			expect(chat.fold(succeeded('t2', 4, extra)), JSON.stringify(extra)).toBe(after);
		}
	});

	it('is not cleared when the failed turn itself later reports completed', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise();
		expect(chat.fold([ev('turn.completed', 4, { turn_id: 't1' })])).toBe('shown');
	});

	it('clears it once a rewind cuts the failed turn away (#1082), not when the cut is after it', () => {
		const cut = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		cut.raise();
		expect(cut.fold([ev('transcript.rewound', 7, { from_seq: 5 })])).toBe('cleared');

		const after = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		after.raise();
		expect(after.fold([...succeeded('t2', 7, { api_error_status: 429 }), ev('transcript.rewound', 10, { from_seq: 8 })])).toBe('shown');
	});

	it('waits for its failure when the frame beats the feed, then follows that failure', () => {
		const chat = new Chat(prompt('t1', 1));
		expect(chat.raise()).toBe('shown');
		expect(chat.replay([[abort('t1', 3)], prompt('t2', 4), [ev('turn.completed', 6, { turn_id: 't2' })]]))
			.toEqual(['shown', 'shown', 'cleared']);
	});

	it('a frame naming an outlived turn raises nothing', () => {
		const chat = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		expect(chat.raise('t1')).toBe('never raised');
	});

	it('matches by the frame\'s turn id when it carries one', () => {
		// Two failures with the same words: the frame is t1's, and only t2's is cut away.
		const chat = new Chat([...failed('t1', 1), ...failed('t2', 4)]);
		chat.raise('t1');
		expect(chat.fold([ev('transcript.rewound', 7, { from_seq: 5 })])).toBe('shown');
		expect(chat.fold([ev('transcript.rewound', 8, { from_seq: 2 })])).toBe('cleared');
	});

	it('leaves any other banner alone, and a banner with no anchor', () => {
		const chat = new Chat(failed('t1', 1));
		chat.raise();
		chat.error = 'This session was taken over by another device.';
		expect(chat.fold(succeeded('t2', 4))).toBe('shown');
		expect(followTurnError(null, FAILED, { caughtUp: true })).toBeNull();
		expect(raiseTurnError('', null, { caughtUp: true })).toBeNull();
	});
});

describe('turn-error banner while the feed replays (review S2)', () => {
	// The current failure's frame arrives with no turn id while the feed is
	// behind; the replay then brings an old failure with the same words, its
	// successful successor, and only then the current failure.
	const oldFailure = failed('t0', 1);
	const oldSuccess = succeeded('tx', 4);
	const current = failed('t1', 7);

	it('keeps the banner through every fold of the replay, then follows the current failure', () => {
		const chat = new Chat([], false);
		expect(chat.raise()).toBe('shown');
		expect(chat.replay([oldFailure, oldSuccess, current])).toEqual(['shown', 'shown', 'shown']);
		// The sentinel: the log is complete.
		expect(chat.fold([], true)).toBe('shown');
		expect(chat.anchor?.failure).toEqual({ seq: 9, turnId: 't1' });
		expect(chat.fold(succeeded('t2', 10))).toBe('cleared');
	});

	it('the same replay with the frame naming its turn', () => {
		const chat = new Chat([], false);
		expect(chat.raise('t1')).toBe('shown');
		expect(chat.replay([oldFailure, oldSuccess, current, []])).toEqual(['shown', 'shown', 'shown', 'shown']);
		expect(chat.fold(succeeded('t2', 10), true)).toBe('cleared');
	});

	it('is not cleared by an old success replayed after the frame, in one batch or many', () => {
		const oneBatch = new Chat([], false);
		oneBatch.raise();
		expect(oneBatch.fold([...succeeded('t0', 1), ...failed('t1', 5)], true)).toBe('shown');

		const perEvent = new Chat([], false);
		perEvent.raise();
		const log = [...succeeded('t0', 1), ...failed('t1', 5)];
		expect(perEvent.replay(log.map((e) => [e]))).toEqual(log.map(() => 'shown'));
		expect(perEvent.fold([], true)).toBe('shown');
	});

	it('shows a message-only frame whose words match an outlived failure: it may be a new run not yet reduced', () => {
		// This client never saw t3 running (another device sent it; or its socket
		// was down), and the frame beats t3's abort.
		const chat = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		expect(chat.raise()).toBe('shown');
		expect(chat.replay([prompt('t3', 7), [abort('t3', 9)]])).toEqual(['shown', 'shown']);
		expect(chat.anchor?.failure).toEqual({ seq: 9, turnId: 't3' });
		expect(chat.fold(succeeded('t4', 10))).toBe('cleared');
	});

	it('a late repeat of an outlived failure stays only until the next clean finish or rewind', () => {
		const finish = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		finish.raise();
		expect(finish.replay([prompt('t3', 7), [ev('turn.completed', 9, { turn_id: 't3' })]])).toEqual(['shown', 'cleared']);

		const rewind = new Chat([...failed('t1', 1), ...succeeded('t2', 4)]);
		rewind.raise();
		expect(rewind.fold([ev('transcript.rewound', 7, { from_seq: 4 })])).toBe('cleared');
	});

	it('a reconnect gap replayed at live cadence decides nothing until its sentinel', () => {
		// Caught up, banner bound to t1; the connection drops and its gap replays.
		const chat = new Chat(failed('t1', 1));
		chat.raise();
		expect(chat.replay([succeeded('t2', 4), failed('t3', 7)], false)).toEqual(['shown', 'shown']);
		expect(chat.fold([], true)).toBe('shown');
		expect(chat.anchor?.failure).toEqual({ seq: 9, turnId: 't3' });
	});
});

describe('turn-error banner after its failure leaves the recent list (review S6)', () => {
	const others = (from: number, count: number) =>
		Array.from({ length: count }, (_, i) => failed(`o${i}`, from + i * 3, `other failure ${i}`)).flat();

	it('a clean finish still clears it after 16 later failures with other words', () => {
		for (const turnId of ['t1', null]) {
			const chat = new Chat(failed('t1', 1));
			chat.raise(turnId);
			expect(chat.fold(others(4, 16))).toBe('shown');
			expect(chat.state.turnOutcomes?.failures.some((f) => f.turnId === 't1')).toBe(false);
			expect(chat.fold(succeeded('t2', 100)), `frame turn id ${turnId}`).toBe('cleared');
		}
	});

	it('a rewind still clears it after eviction when it cuts the failure, and not when it cuts later', () => {
		const cut = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		cut.raise('t1');
		cut.fold(others(7, 16));
		expect(cut.fold([ev('transcript.rewound', 100, { from_seq: 4 })])).toBe('cleared');

		const later = new Chat([...succeeded('t0', 1), ...failed('t1', 4)]);
		later.raise('t1');
		later.fold(others(7, 16));
		expect(later.fold([ev('transcript.rewound', 100, { from_seq: 8 })])).toBe('shown');
		// A deeper cut after a shallow one still reaches it.
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
