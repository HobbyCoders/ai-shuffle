/**
 * Behavior tests for the AskUserQuestion modal's two escape hatches:
 *
 *  - the header "X", which resolves the pending tool call as *dismissed*
 *    instead of leaving it hanging until the question TTL expires; and
 *  - the per-question "Skip", which reports that one question as unanswered
 *    while the rest of a multi-question form still submits normally.
 *
 * Both ride the same `onRespond` callback the ordinary submit path uses — the
 * shape of that payload is the contract the backend maps onto the tool result,
 * so it is what these tests pin down.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import UserQuestion from './UserQuestion.svelte';
import type { UserQuestionData, Question } from './UserQuestion.svelte';

function makeQuestion(over: Partial<Question> = {}): Question {
	return {
		question: 'Which database should we use?',
		header: 'Database',
		options: [
			{ label: 'Postgres', description: 'Relational' },
			{ label: 'SQLite', description: 'Embedded' }
		],
		multiSelect: false,
		...over
	};
}

function makeData(questions: Question[]): UserQuestionData {
	return {
		request_id: 'question-abc',
		tool_use_id: 'toolu_123',
		questions
	};
}

afterEach(() => {
	cleanup();
});

describe('UserQuestion — dismiss ("X")', () => {
	it('renders a dismiss control in the header', () => {
		render(UserQuestion, { props: { data: makeData([makeQuestion()]) } });
		expect(screen.getByLabelText('Dismiss question without answering')).toBeTruthy();
	});

	it('resolves the tool call as dismissed with no answers', async () => {
		const onRespond = vi.fn();
		render(UserQuestion, { props: { data: makeData([makeQuestion()]), onRespond } });

		await fireEvent.click(screen.getByLabelText('Dismiss question without answering'));

		expect(onRespond).toHaveBeenCalledTimes(1);
		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'question-abc',
			tool_use_id: 'toolu_123',
			answers: {},
			dismissed: true
		});
	});

	it('dismisses even when the user has a partial answer in progress', async () => {
		const onRespond = vi.fn();
		render(UserQuestion, { props: { data: makeData([makeQuestion()]), onRespond } });

		await fireEvent.click(screen.getByText('Postgres'));
		await fireEvent.click(screen.getByLabelText('Dismiss question without answering'));

		const detail = onRespond.mock.calls[0][0];
		expect(detail.dismissed).toBe(true);
		expect(detail.answers).toEqual({});
	});
});

describe('UserQuestion — per-question skip', () => {
	it('offers a skip control that is not the primary action', () => {
		render(UserQuestion, { props: { data: makeData([makeQuestion()]) } });
		const skip = screen.getByRole('button', { name: 'Skip this question' });
		expect(skip).toBeTruthy();
		expect(skip.getAttribute('aria-pressed')).toBe('false');
		// The primary action stays the submit button, not the skip control.
		expect(skip.classList.contains('submit-btn')).toBe(false);
	});

	it('marks the question skipped and lets it be undone', async () => {
		render(UserQuestion, { props: { data: makeData([makeQuestion()]) } });

		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		const undo = screen.getByRole('button', { name: 'Skipped — undo' });
		expect(undo.getAttribute('aria-pressed')).toBe('true');

		await fireEvent.click(undo);
		expect(screen.getByRole('button', { name: 'Skip this question' })).toBeTruthy();
	});

	it('enables submit for a single skipped question and reports it as skipped', async () => {
		const onRespond = vi.fn();
		render(UserQuestion, { props: { data: makeData([makeQuestion()]), onRespond } });

		const submit = screen.getByRole('button', { name: 'Submit' }) as HTMLButtonElement;
		expect(submit.disabled).toBe(true);

		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		expect((screen.getByRole('button', { name: 'Submit' }) as HTMLButtonElement).disabled).toBe(
			false
		);

		await fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'question-abc',
			tool_use_id: 'toolu_123',
			answers: {},
			skipped: ['Which database should we use?']
		});
	});

	it('clears an existing selection when the question is skipped', async () => {
		const onRespond = vi.fn();
		render(UserQuestion, { props: { data: makeData([makeQuestion()]), onRespond } });

		await fireEvent.click(screen.getByText('Postgres'));
		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

		const detail = onRespond.mock.calls[0][0];
		expect(detail.answers).toEqual({});
		expect(detail.skipped).toEqual(['Which database should we use?']);
	});

	it('un-skips implicitly when the user picks an option afterwards', async () => {
		const onRespond = vi.fn();
		render(UserQuestion, { props: { data: makeData([makeQuestion()]), onRespond } });

		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		await fireEvent.click(screen.getByText('SQLite'));
		await fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'question-abc',
			tool_use_id: 'toolu_123',
			answers: { 'Which database should we use?': 'SQLite' },
			skipped: []
		});
	});

	it('submits cleanly when some questions are skipped and others answered', async () => {
		const onRespond = vi.fn();
		const data = makeData([
			makeQuestion(),
			makeQuestion({
				question: 'Which cache?',
				header: 'Cache',
				options: [
					{ label: 'Redis', description: 'In-memory' },
					{ label: 'None', description: 'Skip caching' }
				]
			})
		]);
		render(UserQuestion, { props: { data, onRespond } });

		// Q1: answer normally, advance.
		await fireEvent.click(screen.getByText('Postgres'));
		await fireEvent.click(screen.getByRole('button', { name: 'Next' }));

		// Q2: skip, then go to the review step.
		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Review' }));

		// The review step reflects the skip rather than showing a blank answer.
		expect(screen.getByText('Skipped')).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'question-abc',
			tool_use_id: 'toolu_123',
			answers: { 'Which database should we use?': 'Postgres' },
			skipped: ['Which cache?']
		});
	});
});
