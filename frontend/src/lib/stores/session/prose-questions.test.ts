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

describe('cleared by a later user message from anywhere (#2249)', () => {
	const T0 = '2026-10-11T10:00:00.000000+00:00';
	const has = (id: string) => get(proseQuestionSessions).has(id);

	it('clears when another device answers mid-turn (a later last_user_message_at)', () => {
		rememberLeavingProseQuestion('A', asked, T0);
		observeLastUserMessage('A', '2026-10-11T10:05:00.123456+00:00');
		expect(has('A')).toBe(false);
	});

	it('keeps the mark for an equal or older timestamp', () => {
		rememberLeavingProseQuestion('A', asked, T0);
		observeLastUserMessage('A', T0);
		observeLastUserMessage('A', '2026-10-11T10:00:00Z');
		observeLastUserMessage('A', '2026-10-11T09:59:59Z');
		expect(has('A')).toBe(true);
	});

	it('compares instants, not strings (offsets)', () => {
		rememberLeavingProseQuestion('A', asked, '2026-10-11T12:00:00+02:00');
		observeLastUserMessage('A', '2026-10-11T10:00:00Z');
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:01Z');
		expect(has('A')).toBe(false);
	});

	it('ignores row updates without the field (working time, streaming)', () => {
		rememberLeavingProseQuestion('A', asked, T0);
		observeSessionRows([{ id: 'A' }, { id: 'B', last_user_message_at: '2026-10-12T00:00:00Z' }]);
		expect(has('A')).toBe(true);
		const before = get(proseQuestionSessions);
		observeLastUserMessage('A', 'not a date');
		expect(get(proseQuestionSessions)).toBe(before);
	});

	it('clears on null -> non-null, not on null -> null', () => {
		rememberLeavingProseQuestion('A', asked, null);
		observeLastUserMessage('A', null);
		expect(has('A')).toBe(true);
		observeSessionRows([{ id: 'A', last_user_message_at: T0 }]);
		expect(has('A')).toBe(false);
	});

	it('never takes a revision that arrives after leaving as the baseline when nothing was known', () => {
		rememberLeavingProseQuestion('A', asked);
		// The first post-leave value may itself be the answer: it is not adopted…
		observeLastUserMessage('A', T0);
		observeLastUserMessage('A', '2026-10-11T10:00:01Z');
		// …so the entry waits for a reopen rather than guessing either way.
		expect(has('A')).toBe(true);
	});

	it('uses the open session\'s own revision when no row is loaded, so the first answer clears it', () => {
		// Opened from a deep link / Spotlight: its /live snapshot said T0, no sidebar row.
		const claim = beginUserMessageRevisionSeed('A');
		finishUserMessageRevisionSeed('A', claim, T0);
		rememberLeavingProseQuestion('A', asked);
		// The very first broadcast after leaving IS the answer.
		observeLastUserMessage('A', '2026-10-11T10:00:00.000001Z');
		expect(has('A')).toBe(false);
	});

	it('uses a revision broadcast while the session was open when no row is loaded', () => {
		observeLastUserMessage('A', T0);
		rememberLeavingProseQuestion('A', asked);
		observeSessionRows([{ id: 'A', last_user_message_at: T0 }]);
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:05Z');
		expect(has('A')).toBe(false);
	});

	it('retains a loaded row\'s revision without a mark, so a row filtered out before leaving still has a baseline', () => {
		// A is open and its row is loaded; then a tag filter drops the row.
		observeSessionRows([{ id: 'A', last_user_message_at: T0 }, { id: 'B', last_user_message_at: null }]);
		observeSessionRows([{ id: 'B', last_user_message_at: null }]);
		rememberLeavingProseQuestion('A', asked);
		expect(has('A')).toBe(true);
		// The first broadcast after leaving is the answer and clears it.
		observeLastUserMessage('A', '2026-10-11T10:00:00.000001Z');
		expect(has('A')).toBe(false);
	});

	it('keeps the later of the row and the revision seen while open', () => {
		// The row lags behind a message this tab saw broadcast while open.
		observeLastUserMessage('A', '2026-10-11T10:00:30Z');
		rememberLeavingProseQuestion('A', asked, T0);
		observeLastUserMessage('A', '2026-10-11T10:00:30Z');
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:31Z');
		expect(has('A')).toBe(false);
	});

	describe('leaving while the visit\'s /live snapshot is still in flight', () => {
		const T1 = '2026-10-11T10:00:07Z';
		// The question was written at 10:00:03, after T0's prompt and before T1's answer.
		const askedAtThree: ChatMessage[] = [
			{ id: 'u1', role: 'user', content: 'Deploy it', timestamp: T0 },
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?', timestamp: '2026-10-11T10:00:03Z' }
		];

		it('snapshot resolves, then the answer arrives: the answer clears', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', asked);
			finishUserMessageRevisionSeed('A', claim, T0);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', T1);
			expect(has('A')).toBe(false);
		});

		it('answer arrives, then the snapshot resolves with the pre-answer value: the held answer clears', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', asked);
			observeLastUserMessage('A', T1);
			expect(has('A')).toBe(true);
			finishUserMessageRevisionSeed('A', claim, T0);
			expect(has('A')).toBe(false);
		});

		it('answer arrives, then a snapshot taken AFTER the answer resolves: it post-dates the question, so it clears', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAtThree);
			observeLastUserMessage('A', T1);
			finishUserMessageRevisionSeed('A', claim, T1);
			expect(has('A')).toBe(false);
		});

		it('a snapshot taken after the answer clears even when the answer\'s broadcast was missed', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAtThree);
			finishUserMessageRevisionSeed('A', claim, T1);
			expect(has('A')).toBe(false);
		});

		it('a pre-question snapshot is the baseline: a refresh of the same value keeps the mark', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAtThree);
			observeLastUserMessage('A', T0);
			finishUserMessageRevisionSeed('A', claim, T0);
			expect(has('A')).toBe(true);
			observeSessionRows([{ id: 'A', last_user_message_at: T0 }]);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', T1);
			expect(has('A')).toBe(false);
		});

		it('an observation recorded during the visit is compared with the late snapshot (missed answer broadcast)', () => {
			const claim = beginUserMessageRevisionSeed('A');
			observeLastUserMessage('A', T0); // a row/broadcast while open
			rememberLeavingProseQuestion('A', askedAtThree);
			finishUserMessageRevisionSeed('A', claim, T1);
			expect(has('A')).toBe(false);
		});

		it('a late snapshot no later than the question never clears an observation recorded during the visit', () => {
			const claim = beginUserMessageRevisionSeed('A');
			observeLastUserMessage('A', '2026-10-11T09:00:00Z');
			rememberLeavingProseQuestion('A', askedAtThree);
			// The prompt that led to the question, whose broadcast this tab missed.
			finishUserMessageRevisionSeed('A', claim, T0);
			expect(has('A')).toBe(true);
		});

		it('a departed visit\'s late snapshot never resolves a newer visit\'s baseline', () => {
			const first = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', askedAtThree);
			// Answered elsewhere at T1; reopened here with a NEW question at 10:00:09.
			const second = beginUserMessageRevisionSeed('A');
			const askedAgain: ChatMessage[] = [
				...askedAtThree,
				{ id: 'u2', role: 'user', content: 'Prod only', timestamp: T1 },
				{ id: 'a2', role: 'assistant', type: 'text', content: 'Which region?', timestamp: '2026-10-11T10:00:09Z' }
			];
			rememberLeavingProseQuestion('A', askedAgain);
			observeLastUserMessage('A', T1); // the answer's broadcast, after leaving again
			finishUserMessageRevisionSeed('A', second, T1);
			finishUserMessageRevisionSeed('A', first, T0);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', '2026-10-11T10:00:12Z');
			expect(has('A')).toBe(false);
		});

		it('a departed visit\'s snapshot resolving first is ignored as a baseline too', () => {
			const first = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', asked);
			const second = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', asked);
			finishUserMessageRevisionSeed('A', first, T0);
			// Still waiting on the current visit: a post-leave value is held, not adopted.
			observeLastUserMessage('A', T1);
			expect(has('A')).toBe(true);
			finishUserMessageRevisionSeed('A', second, T1);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', '2026-10-11T10:00:08Z');
			expect(has('A')).toBe(false);
		});

		it('a failed snapshot leaves the entry waiting for a reopen', () => {
			const claim = beginUserMessageRevisionSeed('A');
			rememberLeavingProseQuestion('A', asked);
			observeLastUserMessage('A', T1);
			finishUserMessageRevisionSeed('A', claim, undefined);
			expect(has('A')).toBe(true);
			observeLastUserMessage('A', '2026-10-11T10:00:08Z');
			expect(has('A')).toBe(true);
		});

		it('forgetting the session (cleared or deleted) drops its claim', () => {
			const claim = beginUserMessageRevisionSeed('A');
			forgetProseQuestion('A');
			rememberLeavingProseQuestion('A', asked);
			finishUserMessageRevisionSeed('A', claim, T0);
			// No claim: the late snapshot is not adopted as the baseline.
			observeLastUserMessage('A', T1);
			expect(has('A')).toBe(true);
		});
	});

	it('clears on a strictly later revision within the same millisecond', () => {
		rememberLeavingProseQuestion('A', asked, '2026-10-11T10:00:00.000001+00:00');
		observeLastUserMessage('A', '2026-10-11T10:00:00.000999+00:00');
		expect(has('A')).toBe(false);
	});

	it('keeps it for the same instant written with another offset or fraction length', () => {
		rememberLeavingProseQuestion('A', asked, '2026-10-11T10:00:00.5+00:00');
		observeLastUserMessage('A', '2026-10-11T06:00:00.500000-04:00');
		observeLastUserMessage('A', '2026-10-11T10:00:00.500Z');
		observeLastUserMessage('A', '2026-10-11T12:00:00.50000+02:00');
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T06:00:00.500001-04:00');
		expect(has('A')).toBe(false);
	});

	it('re-records the observation when the session is reopened and left again', () => {
		rememberLeavingProseQuestion('A', asked, T0);
		// Reopened here; the answer and a new question arrived, then left.
		const later = '2026-10-11T11:00:00Z';
		rememberLeavingProseQuestion('A', [...answeredTranscript, { id: 'a2', role: 'assistant', type: 'text', content: 'Which region?' }], later);
		observeLastUserMessage('A', '2026-10-11T10:30:00Z');
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T11:00:01Z');
		expect(has('A')).toBe(false);
	});

	it('forgets the observation with the entry, so a later update cannot resurrect or clear it', () => {
		rememberLeavingProseQuestion('A', asked, T0);
		forgetProseQuestion('A');
		const before = get(proseQuestionSessions);
		observeLastUserMessage('A', '2026-10-12T00:00:00Z');
		expect(get(proseQuestionSessions)).toBe(before);
		expect(has('A')).toBe(false);
	});
});
