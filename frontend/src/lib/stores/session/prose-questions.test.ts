import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	_resetProseQuestions,
	forgetProseQuestion,
	noteUserMessageRevision,
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

	it('takes the first value seen as the baseline when the row was not loaded at leave time', () => {
		rememberLeavingProseQuestion('A', asked);
		observeSessionRows([{ id: 'A', last_user_message_at: T0 }]);
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:01Z');
		expect(has('A')).toBe(false);
	});

	it('uses the open session\'s own revision when no row is loaded, so the first answer clears it', () => {
		// Opened from a deep link / Spotlight: its detail said T0, no sidebar row.
		noteUserMessageRevision('A', T0);
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

	it('keeps the later of the row and the revision seen while open', () => {
		// The row lags behind a message this tab saw broadcast while open.
		observeLastUserMessage('A', '2026-10-11T10:00:30Z');
		rememberLeavingProseQuestion('A', asked, T0);
		observeLastUserMessage('A', '2026-10-11T10:00:30Z');
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:31Z');
		expect(has('A')).toBe(false);
	});

	it('a detail that arrives after leaving supplies a missing baseline but never clears', () => {
		rememberLeavingProseQuestion('A', asked);
		noteUserMessageRevision('A', T0);
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', T0);
		expect(has('A')).toBe(true);
		observeLastUserMessage('A', '2026-10-11T10:00:01Z');
		expect(has('A')).toBe(false);
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
