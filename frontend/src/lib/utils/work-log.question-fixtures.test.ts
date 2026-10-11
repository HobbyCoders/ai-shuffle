import { describe, expect, it } from 'vitest';
import cases from '../../../../tests/fixtures/prose_questions.json';
import { asksQuestion, lastProseAsksQuestion } from './work-log';

// Also consumed by tests/db/test_session_assistant_question_revision.py.
describe('backend/frontend shared prose question predicate', () => {
	it.each(cases)('$content', ({ content, expected }) => {
		expect(asksQuestion(content)).toBe(expected);
		expect(lastProseAsksQuestion([{ id: 'prose', role: 'assistant', type: 'text', content }])).toBe(expected);
	});
});
