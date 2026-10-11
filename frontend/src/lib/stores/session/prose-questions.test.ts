import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	_resetProseQuestions,
	forgetProseQuestion,
	proseQuestionPending,
	proseQuestionSessions,
	reconcileProseQuestions,
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
	it('records a leaving session whose last prose asks, and clears one that no longer does', () => {
		rememberLeavingProseQuestion('A', asked, { workStartedAt: null, streaming: false });
		expect(get(proseQuestionSessions).has('A')).toBe(true);
		rememberLeavingProseQuestion('A', answeredTranscript, { workStartedAt: null, streaming: false });
		expect(get(proseQuestionSessions).has('A')).toBe(false);
	});

	it('keeps counting until the session list reports a newer turn', () => {
		const mark = { workStartedAt: null, streaming: false };
		expect(proseQuestionPending(mark, { work_started_at: null }, false)).toBe(true);
		expect(proseQuestionPending(mark, { work_started_at: '2026-10-11T00:00:00Z' }, true)).toBe(false);
	});

	it('a question seen mid-turn survives that turn ending and clears on the next one', () => {
		const mark = { workStartedAt: 'T1', streaming: true };
		expect(proseQuestionPending(mark, { work_started_at: 'T1' }, true)).toBe(true);
		expect(proseQuestionPending(mark, { work_started_at: null }, false)).toBe(true);
		expect(proseQuestionPending(mark, { work_started_at: 'T2' }, true)).toBe(false);
	});

	it('adopts the turn start of a question seen before the list reported it', () => {
		rememberLeavingProseQuestion('A', asked, { workStartedAt: null, streaming: true });
		reconcileProseQuestions([{ id: 'A', work_started_at: 'T1' }], new Map([['A', {}]]));
		expect(get(proseQuestionSessions).get('A')).toEqual({ workStartedAt: 'T1', streaming: true });
		reconcileProseQuestions([{ id: 'A', work_started_at: null }], new Map());
		expect(get(proseQuestionSessions).has('A')).toBe(true);
		reconcileProseQuestions([{ id: 'A', work_started_at: 'T2' }], new Map([['A', {}]]));
		expect(get(proseQuestionSessions).has('A')).toBe(false);
	});

	it('drops the entry once answered so a later settle does not revive it', () => {
		rememberLeavingProseQuestion('A', asked, { workStartedAt: null, streaming: false });
		reconcileProseQuestions([{ id: 'A', work_started_at: 'T2' }], new Map([['A', {}]]));
		reconcileProseQuestions([{ id: 'A', work_started_at: null }], new Map());
		expect(get(proseQuestionSessions).has('A')).toBe(false);
	});

	it('falls back to the live-run map for a row without the turn-start field', () => {
		const mark = { workStartedAt: null, streaming: false };
		expect(proseQuestionPending(mark, {}, false)).toBe(true);
		expect(proseQuestionPending(mark, {}, true)).toBe(false);
	});

	it('forgets a deleted session, and leaves rows absent from the list alone', () => {
		rememberLeavingProseQuestion('A', asked, { workStartedAt: null, streaming: false });
		rememberLeavingProseQuestion('B', asked, { workStartedAt: null, streaming: false });
		reconcileProseQuestions([], new Map());
		expect(get(proseQuestionSessions).size).toBe(2);
		forgetProseQuestion('A');
		expect([...get(proseQuestionSessions).keys()]).toEqual(['B']);
	});

	it('withoutSession returns the same map when the session has no entry', () => {
		rememberLeavingProseQuestion('A', asked, { workStartedAt: null, streaming: false });
		const map = get(proseQuestionSessions);
		expect(withoutSession(map, 'B')).toBe(map);
		expect(withoutSession(map, 'A').has('A')).toBe(false);
	});
});
