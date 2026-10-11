import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	_resetProseQuestions,
	forgetProseQuestion,
	beginUserMessageRevisionSeed,
	finishUserMessageRevisionSeed,
	observeLastUserMessage,
	observeSessionRows,
	proseQuestionSessions,
	rememberLeavingProseQuestion,
	withoutSession
} from './prose-questions';
import type { ChatMessage } from '../types';

const asked: ChatMessage[] = [
	{ id: 'u1', role: 'user', content: 'Deploy it' },
	{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?' }
];
const answeredTranscript: ChatMessage[] = [...asked, { id: 'u2', role: 'user', content: 'Prod only' }];

afterEach(_resetProseQuestions);

describe('remembered prose questions (spec §5)', () => {
	it('records a leaving question and drops it when a loaded transcript shows the answer', () => {
		rememberLeavingProseQuestion('A', asked);
		expect(get(proseQuestionSessions).has('A')).toBe(true);
		rememberLeavingProseQuestion('A', answeredTranscript);
		expect(get(proseQuestionSessions).has('A')).toBe(false);
	});

	it('does not publish a new set when nothing changes', () => {
		rememberLeavingProseQuestion('A', asked);
		const before = get(proseQuestionSessions);
		rememberLeavingProseQuestion('A', asked);
		rememberLeavingProseQuestion('B', answeredTranscript);
		forgetProseQuestion('C');
		expect(get(proseQuestionSessions)).toBe(before);
	});

	it('forgets a deleted session only', () => {
		rememberLeavingProseQuestion('A', asked);
		rememberLeavingProseQuestion('B', asked);
		forgetProseQuestion('A');
		expect([...get(proseQuestionSessions)]).toEqual(['B']);
	});

	it('withoutSession returns the same set when the session is not in it', () => {
		rememberLeavingProseQuestion('A', asked);
		const set = get(proseQuestionSessions);
		expect(withoutSession(set, 'B')).toBe(set);
		expect(withoutSession(set, null)).toBe(set);
		expect(withoutSession(set, 'A').has('A')).toBe(false);
	});
});

describe('cleared by a later user message in the question\'s log (#2249)', () => {
	const E = 'evt-epoch-1';
	const has = (id: string) => get(proseQuestionSessions).has(id);
	const rev = (seq: number | null, epoch: string | null = E) => ({
		last_user_message_seq: seq,
		last_user_message_epoch: epoch
	});
	/** A transcript whose trailing question was rendered from event `seq`. */
	const askedAt = (seq: number | null, epoch: string | null = E): ChatMessage[] => [
		{ id: 'u1', role: 'user', content: 'Deploy it', seq: seq === null ? null : seq - 1, epoch },
		{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?', seq, epoch }
	];

	it('clears when another device answers (a later seq in the same epoch)', () => {
		rememberLeavingProseQuestion('A', askedAt(10));
		observeLastUserMessage('A', rev(11));
		expect(has('A')).toBe(false);
	});

	it('keeps the mark for an equal or lower seq', () => {
		rememberLeavingProseQuestion('A', askedAt(10));
		observeLastUserMessage('A', rev(10));
		observeLastUserMessage('A', rev(9));
		observeLastUserMessage('A', rev(0));
		expect(has('A')).toBe(true);
	});

	it('same-transaction voice exchange: the prompt (seq 1) does not answer the question (seq 2); seq 3 does', () => {
		// The voice prompt and its spoken reply are persisted together, so they
		// share one created_at; only the event order tells them apart.
		rememberLeavingProseQuestion('A', askedAt(2));
		observeLastUserMessage('A', rev(1));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(3));
		expect(has('A')).toBe(false);
	});

	it('a delayed pre-question revision arriving after leaving never clears', () => {
		rememberLeavingProseQuestion('A', askedAt(8));
		// A /live snapshot or list refresh taken before the question.
		observeLastUserMessage('A', rev(5));
		observeLastUserMessage('A', rev(7));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(9));
		expect(has('A')).toBe(false);
	});

	it('stale observation trace: a late pre-question snapshot, then a refresh of that value, keeps the mark', () => {
		// Seen during the visit (an earlier user message), then the question.
		observeLastUserMessage('A', rev(4));
		const claim = beginUserMessageRevisionSeed('A');
		rememberLeavingProseQuestion('A', askedAt(6));
		// The visit's snapshot lands late with the pre-question value...
		finishUserMessageRevisionSeed('A', claim, rev(4));
		// ...and a later list refresh carries the same value.
		observeSessionRows([{ id: 'A', ...rev(4) }]);
		observeLastUserMessage('A', rev(4));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(7));
		expect(has('A')).toBe(false);
	});

	it('the highest seq seen is kept: a stale lower revision cannot move it back', () => {
		observeLastUserMessage('A', rev(12));
		observeLastUserMessage('A', rev(3));
		// The transcript lagged: the answer (seq 12) was already seen here.
		rememberLeavingProseQuestion('A', askedAt(10));
		expect(has('A')).toBe(false);
	});

	it('does not mark a question this tab already saw answered elsewhere', () => {
		rememberLeavingProseQuestion('A', askedAt(10), rev(11));
		expect(has('A')).toBe(false);
	});

	it('cross-epoch: a high seq from another log (old epoch 20 vs new question 2) never clears', () => {
		rememberLeavingProseQuestion('A', askedAt(2, 'evt-new'));
		observeLastUserMessage('A', rev(20, 'evt-old'));
		expect(has('A')).toBe(true);
		// Nor does a revision already seen in the old log mark it answered at leave.
		rememberLeavingProseQuestion('A', askedAt(2, 'evt-new'));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(3, 'evt-new'));
		expect(has('A')).toBe(false);
	});

	it('a question without a position never clears on a revision alone', () => {
		rememberLeavingProseQuestion('A', askedAt(null, null));
		observeLastUserMessage('A', rev(100));
		expect(has('A')).toBe(true);
		// The rendered-answer path still works.
		rememberLeavingProseQuestion('A', answeredTranscript);
		expect(has('A')).toBe(false);
	});

	it('a question with a seq but no epoch never clears on a revision alone', () => {
		rememberLeavingProseQuestion('A', askedAt(5, null));
		observeLastUserMessage('A', rev(6));
		expect(has('A')).toBe(true);
	});

	it('a revision with a null seq or epoch never clears (legacy write, cleared log, older worker)', () => {
		rememberLeavingProseQuestion('A', askedAt(5));
		observeLastUserMessage('A', rev(null));
		observeLastUserMessage('A', rev(9, null));
		observeLastUserMessage('A', { last_user_message_at: '2026-10-11T10:05:00Z' } as never);
		observeLastUserMessage('A', rev(9, ''));
		observeLastUserMessage('A', { last_user_message_seq: '9', last_user_message_epoch: E });
		observeLastUserMessage('A', rev(6.5));
		expect(has('A')).toBe(true);
	});

	it('the question position is the last contributing event, so a revision between the first and last delta does not clear', () => {
		// The question started streaming at seq 4 and its last delta was seq 7.
		rememberLeavingProseQuestion('A', askedAt(7));
		observeLastUserMessage('A', rev(6));
		expect(has('A')).toBe(true);
	});

	it('observes rows, skipping unchanged ones, and clears from a later row', () => {
		rememberLeavingProseQuestion('A', askedAt(5));
		observeSessionRows([{ id: 'A', ...rev(5) }, { id: 'B', ...rev(1) }]);
		expect(has('A')).toBe(true);
		observeSessionRows([{ id: 'A', ...rev(5) }]);
		expect(has('A')).toBe(true);
		observeSessionRows([{ id: 'A', ...rev(6) }]);
		expect(has('A')).toBe(false);
	});

	it('ignores row updates without the fields (working time, streaming)', () => {
		rememberLeavingProseQuestion('A', askedAt(5));
		observeLastUserMessage('A', { work_ms: 10 } as never);
		observeSessionRows([{ id: 'A' }]);
		expect(has('A')).toBe(true);
	});

	it('re-records the question when the session is reopened and left again', () => {
		rememberLeavingProseQuestion('A', askedAt(5));
		observeLastUserMessage('A', rev(6));
		expect(has('A')).toBe(false);
		// A new question later in the same log.
		rememberLeavingProseQuestion('A', askedAt(9));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(6));
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', rev(10));
		expect(has('A')).toBe(false);
	});

	it('forgets everything with the session, so a later update cannot clear or resurrect it', () => {
		observeLastUserMessage('A', rev(20));
		forgetProseQuestion('A');
		rememberLeavingProseQuestion('A', askedAt(10));
		expect(has('A')).toBe(true);
	});

	describe('per-visit /live claims', () => {
		it('the visit\'s snapshot clears when it carries the answer', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(5));
			finishUserMessageRevisionSeed('A', claim, rev(6));
			expect(has('A')).toBe(false);
		});

		it('a failed snapshot leaves the mark waiting', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(5));
			finishUserMessageRevisionSeed('A', claim, undefined);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', rev(6));
			expect(has('A')).toBe(false);
		});

		it('overlapping visits, departed snapshot last: neither pre-question snapshot clears the new question', () => {
			const first = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(3));
			const second = beginUserMessageRevisionSeed('A');
			// A new question was asked during the second visit.
			rememberLeavingProseQuestion('A', askedAt(8));
			finishUserMessageRevisionSeed('A', second, rev(7));
			finishUserMessageRevisionSeed('A', first, rev(2));
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', rev(9));
			expect(has('A')).toBe(false);
		});

		it('overlapping visits, departed snapshot first: same outcome', () => {
			const first = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(3));
			const second = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(8));
			finishUserMessageRevisionSeed('A', first, rev(2));
			finishUserMessageRevisionSeed('A', second, rev(7));
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', rev(9));
			expect(has('A')).toBe(false);
		});

		it('a departed visit\'s late snapshot that answers the current question still clears it', () => {
			const first = beginUserMessageRevisionSeed('A');
			beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAt(8));
			finishUserMessageRevisionSeed('A', first, rev(9));
			expect(has('A')).toBe(false);
		});

		it('forgetting the session drops its claim', () => {
			const claim = beginUserMessageRevisionSeed('A');
			forgetProseQuestion('A');
			rememberLeavingProseQuestion('A', askedAt(5));
			finishUserMessageRevisionSeed('A', claim, rev(4));
			expect(has('A')).toBe(true);
		});
	});
});

describe('row-derived assistant question (#2263)', () => {
	const E = 'evt-epoch-1';
	const E2 = 'evt-epoch-2';
	const has = (id: string) => get(proseQuestionSessions).has(id);
	const row = (
		question: number | null,
		user: number | null,
		qEpoch: string | null = E,
		uEpoch: string | null = user === null ? null : E
	) => ({
		last_assistant_question_seq: question,
		last_assistant_question_epoch: qEpoch,
		last_user_message_seq: user,
		last_user_message_epoch: uEpoch
	});

	it('marks a session this tab never opened when its row asks after the last user message', () => {
		observeSessionRows([{ id: 'A', ...row(12, 9) }]);
		expect(has('A')).toBe(true);
	});

	it('marks when no user message is known yet', () => {
		observeLastUserMessage('A', row(3, null));
		expect(has('A')).toBe(true);
	});

	it('is cleared by later non-question prose (null seq, same epoch)', () => {
		observeLastUserMessage('A', row(12, 9));
		observeLastUserMessage('A', { last_assistant_question_seq: null, last_assistant_question_epoch: E });
		expect(has('A')).toBe(false);
	});

	it('is cleared by a user answer in the same epoch, including a user-only frame', () => {
		observeLastUserMessage('A', row(12, 9));
		observeLastUserMessage('A', { last_user_message_seq: 13, last_user_message_epoch: E });
		expect(has('A')).toBe(false);
		// A delayed revision from before the answer cannot un-answer it.
		observeLastUserMessage('A', row(12, 9));
		expect(has('A')).toBe(false);
	});

	it('re-marks when the agent asks again after the answer', () => {
		observeLastUserMessage('A', row(12, 9));
		observeLastUserMessage('A', { last_user_message_seq: 13, last_user_message_epoch: E });
		observeLastUserMessage('A', row(16, 13));
		expect(has('A')).toBe(true);
	});

	it('does not compare positions across epochs', () => {
		observeLastUserMessage('A', row(12, 2, E, E2));
		expect(has('A')).toBe(false);
		observeLastUserMessage('B', row(5, 40, E2, E));
		expect(has('B')).toBe(false);
	});

	it('explicit null seq unmarks even a transcript-remembered question', () => {
		rememberLeavingProseQuestion('A', [
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Prod only?', seq: 4, epoch: E }
		]);
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', row(null, 3));
		expect(has('A')).toBe(false);
	});

	it('falls back to the transcript when the fields are absent (older worker)', () => {
		rememberLeavingProseQuestion('A', [
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Prod only?', seq: 4, epoch: E }
		]);
		observeSessionRows([{ id: 'A', last_user_message_seq: 3, last_user_message_epoch: E }]);
		expect(has('A')).toBe(true);
		// And an older row without a question never marks on its own.
		observeSessionRows([{ id: 'B', last_user_message_seq: 3, last_user_message_epoch: E }]);
		expect(has('B')).toBe(false);
	});

	it('falls back to the transcript when both question fields are null', () => {
		rememberLeavingProseQuestion('A', [
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Prod only?', seq: 4, epoch: E }
		]);
		observeLastUserMessage('A', row(null, 3, null));
		expect(has('A')).toBe(true);
	});

	it('the row wins over the transcript when leaving the session', () => {
		observeLastUserMessage('A', row(8, 5));
		rememberLeavingProseQuestion('A', [{ id: 'a1', role: 'assistant', type: 'text', content: 'Done.' }]);
		expect(has('A')).toBe(true);
	});

	it('a clear retires the old log: a delayed pre-clear row does not re-mark', () => {
		observeLastUserMessage('A', row(12, 9));
		forgetProseQuestion('A', { cleared: true });
		expect(has('A')).toBe(false);
		observeSessionRows([{ id: 'A', ...row(12, 9) }]);
		expect(has('A')).toBe(false);
		// The new log asks: marked again.
		observeLastUserMessage('A', row(2, null, E2));
		expect(has('A')).toBe(true);
	});

	it('deleting the session drops the row-derived mark', () => {
		observeLastUserMessage('A', row(12, 9));
		forgetProseQuestion('A');
		expect(has('A')).toBe(false);
	});

	it('re-observes a row whose question fields changed', () => {
		observeSessionRows([{ id: 'A', ...row(null, 9) }]);
		expect(has('A')).toBe(false);
		observeSessionRows([{ id: 'A', ...row(12, 9) }]);
		expect(has('A')).toBe(true);
	});
});
