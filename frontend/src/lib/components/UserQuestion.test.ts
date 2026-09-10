import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import UserQuestion, { type UserQuestionData, type Question } from './UserQuestion.svelte';

const question = (over: Partial<Question> = {}): Question => ({
	question: 'Which database should we use?', header: 'Database', multiSelect: false,
	options: [{ label: 'Postgres', description: 'Relational' }, { label: 'SQLite', description: 'Embedded' }], ...over
});
const data = (questions = [question()]): UserQuestionData => ({ request_id: 'q-1', tool_use_id: 'tool-1', questions, nonblocking: true });
afterEach(cleanup);

describe('UserQuestion — nonmodal answer form', () => {
	it('does not render a modal or a dismiss-on-close button', () => {
		render(UserQuestion, { data: data() });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(screen.queryByLabelText('Dismiss question without answering')).toBeNull();
		expect((screen.getByRole('button', { name: 'Send answer' }) as HTMLButtonElement).disabled).toBe(true);
	});
	it('submits a selected answer using full question text as its key', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond });
		await fireEvent.click(screen.getByLabelText('Postgres', { exact: false }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond).toHaveBeenCalledWith({ request_id: 'q-1', tool_use_id: 'tool-1', answers: { 'Which database should we use?': 'Postgres' }, skipped: [] });
	});
	it('keeps free text when selecting an option and gives it precedence', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond });
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: '  Another database  ' } });
		await fireEvent.click(screen.getByLabelText('SQLite', { exact: false }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0].answers).toEqual({ 'Which database should we use?': 'Another database' });
	});
	it('retains a draft across request-object hydration', async () => {
		const props = data(); const view = render(UserQuestion, { data: props });
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: 'My draft' } });
		await view.rerender({ data: { ...props, questions: [...props.questions] } });
		expect((screen.getByLabelText('Or write your own answer') as HTMLTextAreaElement).value).toBe('My draft');
	});
	it('resets drafts for a different request', async () => {
		const view = render(UserQuestion, { data: data() });
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: 'My draft' } });
		await view.rerender({ data: { ...data(), request_id: 'q-2' } });
		expect((screen.getByLabelText('Or write your own answer') as HTMLTextAreaElement).value).toBe('');
	});
	it('does not steal Enter or Escape from the composer/window', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond });
		await fireEvent.click(screen.getByLabelText('Postgres', { exact: false }));
		for (const key of ['Enter', 'Escape']) {
			const event = new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true });
			window.dispatchEvent(event); expect(event.defaultPrevented).toBe(false);
		}
		expect(onRespond).not.toHaveBeenCalled();
	});
	it('keeps multiline Enter in the written answer local', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond });
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: 'First line' } });
		await fireEvent.keyDown(screen.getByLabelText('Or write your own answer'), { key: 'Enter' });
		expect(onRespond).not.toHaveBeenCalled();
	});
	it('supports open-ended questions without options', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data([question({ options: [] })]), onRespond });
		await fireEvent.input(screen.getByLabelText('Your answer'), { target: { value: 'Use the existing database.' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0].answers['Which database should we use?']).toBe('Use the existing database.');
	});
	it('serializes multi-select answers as comma-separated strings', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data([question({ multiSelect: true })]), onRespond });
		await fireEvent.click(screen.getByLabelText('Postgres', { exact: false }));
		await fireEvent.click(screen.getByLabelText('SQLite', { exact: false }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0].answers['Which database should we use?']).toBe('Postgres, SQLite');
	});
	it('lets the user explicitly skip, undo, and replace the skip with an answer', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond });
		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		expect(screen.getByRole('button', { name: 'Skipped — undo' }).getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'Skipped — undo' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		await fireEvent.click(screen.getByLabelText('SQLite', { exact: false }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0]).toMatchObject({ answers: { 'Which database should we use?': 'SQLite' }, skipped: [] });
	});
	it('reports skipped questions out of band alongside the answered batch', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data([question(), question({ question: 'Which cache?', header: 'Cache' })]), onRespond });
		await fireEvent.click(screen.getByLabelText('Postgres', { exact: false }));
		expect((screen.getByRole('button', { name: 'Send answers' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Cache' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Skip this question' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send answers' }));
		expect(onRespond.mock.calls[0][0]).toMatchObject({ answers: { 'Which database should we use?': 'Postgres' }, skipped: ['Which cache?'] });
	});
	it('does not submit while disconnected', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data(), onRespond, disabled: true });
		await fireEvent.submit(screen.getByRole('form'));
		expect(onRespond).not.toHaveBeenCalled();
	});
});
