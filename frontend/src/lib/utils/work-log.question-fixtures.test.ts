import { describe, expect, it } from 'vitest';
import cases from '../../../../tests/fixtures/prose_questions.json';
import { asksQuestion, lastProseAsksQuestion } from './work-log';
import eventCases from '../../../../tests/fixtures/prose_question_events.json';
import { initialState, reduce, type WireEvent } from '$lib/stores/session/v2/reducer';

// Exercise the real bundled reducer at every persisted-event prefix, not a
// handwritten approximation of its streaming and retry behavior.
describe('backend/frontend shared event-sequence projection', () => {
	it.each(eventCases)('$name', ({ events }) => {
		let state = initialState();
		for (const [index, step] of events.entries()) {
			const event = { ...step.payload, type: step.type, id: `event-${index + 1}`,
				seq: index + 1, session_id: 'question' } as WireEvent;
			state = reduce(state, event);
			const asks = lastProseAsksQuestion(state.messages);
			const prose = state.messages.findLast((m) => m.role === 'assistant' &&
				(m.type === 'text' || !m.type) && m.content?.trim());
			expect(asks ? prose?.seq : null, `step ${index + 1}: ${step.type}`).toBe(step.question_seq);
		}
	});
});

// Also consumed by tests/db/test_session_assistant_question_revision.py.
describe('backend/frontend shared prose question predicate', () => {
	it.each(cases)('$content', ({ content, expected }) => {
		expect(asksQuestion(content)).toBe(expected);
		expect(lastProseAsksQuestion([{ id: 'prose', role: 'assistant', type: 'text', content }])).toBe(expected);
	});
});
