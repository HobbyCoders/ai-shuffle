import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import UserQuestion, { type UserQuestionData, type Question } from './UserQuestion.svelte';

// The owner's model list, as the pickers build it from the hydrated registry.
const catalog = vi.hoisted(() => ({
	groups: [{ key: '__claude__', name: 'Claude', subtext: null, glyph: 'claude' as const, isApi: false }],
	options: [
		{ value: 'sonnet', label: 'Sonnet', description: 'sonnet', groupKey: '__claude__', group: 'Claude', groupSubtext: null },
		{ value: 'opus', label: 'Opus', description: 'opus', groupKey: '__claude__', group: 'Claude', groupSubtext: null }
	]
}));
vi.mock('$lib/constants/model-picker', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/model-picker')>()),
	buildModelPickerCatalog: () => catalog
}));

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
	it.each([false, true])('clears choices and highlighting when typing (multiSelect=%s)', async (multiSelect) => {
		const onRespond = vi.fn(); const view = render(UserQuestion, { data: data([question({ multiSelect })]), onRespond });
		const options = screen.getAllByRole(multiSelect ? 'checkbox' : 'radio') as HTMLInputElement[];
		for (const option of options) await fireEvent.click(option);
		expect(options.some(option => option.checked)).toBe(true);
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: '  Another database  ' } });
		expect(options.every(option => !option.checked)).toBe(true);
		expect(view.container.querySelectorAll('.choices label.selected')).toHaveLength(0);
		expect(screen.getByText('Your written answer will be sent.')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0].answers).toEqual({ 'Which database should we use?': 'Another database' });
	});
	it.each([false, true])('switches back to choices without hidden custom text (multiSelect=%s)', async (multiSelect) => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data([question({ multiSelect })]), onRespond });
		const textarea = screen.getByLabelText('Or write your own answer') as HTMLTextAreaElement;
		await fireEvent.input(textarea, { target: { value: 'Another database' } });
		await fireEvent.click(screen.getByLabelText('SQLite', { exact: false }));
		expect(textarea.value).toBe('');
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onRespond.mock.calls[0][0].answers).toEqual({ 'Which database should we use?': 'SQLite' });
	});
	it.each([false, true])('never resurrects selections when custom text is erased (multiSelect=%s)', async (multiSelect) => {
		const onRespond = vi.fn(); render(UserQuestion, { data: data([question({ multiSelect })]), onRespond });
		const options = screen.getAllByRole(multiSelect ? 'checkbox' : 'radio') as HTMLInputElement[];
		for (const option of options) await fireEvent.click(option);
		const textarea = screen.getByLabelText('Or write your own answer');
		for (const value of [' ', 'Custom answer', '']) {
			await fireEvent.input(textarea, { target: { value } });
			expect(options.every(option => !option.checked)).toBe(true);
			expect((screen.getByRole('button', { name: 'Send answer' }) as HTMLButtonElement).disabled).toBe(!value.trim());
		}
		await fireEvent.submit(screen.getByRole('form'));
		expect(onRespond).not.toHaveBeenCalled();
	});
	it('clears only the edited question and retains the state across tabs and hydration', async () => {
		const onRespond = vi.fn();
		const props = data([question(), question({ question: 'Which cache?', header: 'Cache', multiSelect: true })]);
		const view = render(UserQuestion, { data: props, onRespond });
		await fireEvent.click(screen.getByLabelText('Postgres', { exact: false }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cache' }));
		await fireEvent.click(screen.getByLabelText('SQLite', { exact: false }));
		await fireEvent.input(screen.getByLabelText('Or write your own answer'), { target: { value: 'Redis' } });
		await view.rerender({ data: { ...props, questions: [...props.questions] }, onRespond });
		await fireEvent.click(screen.getByRole('button', { name: /Database/ }));
		expect((screen.getByLabelText('Postgres', { exact: false }) as HTMLInputElement).checked).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: /Cache/ }));
		expect((screen.getByLabelText('SQLite', { exact: false }) as HTMLInputElement).checked).toBe(false);
		expect((screen.getByLabelText('Or write your own answer') as HTMLTextAreaElement).value).toBe('Redis');
		await fireEvent.click(screen.getByRole('button', { name: 'Send answers' }));
		expect(onRespond.mock.calls[0][0].answers).toEqual({ 'Which database should we use?': 'Postgres', 'Which cache?': 'Redis' });
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

describe('UserQuestion — model picker question (#1827)', () => {
	const modelQuestion = (): UserQuestionData => ({
		request_id: 'subagent-default-1', tool_use_id: 'subagent-default-1', questions: [{
			question: 'Which model should subagents use?', header: 'Subagent model',
			options: [], multiSelect: false, picker: 'model'
		}]
	});
	const option = (label: string) => screen.getAllByRole('option')
		.find((row) => row.textContent?.trim().startsWith(label)) as HTMLElement;

	it('lists the owner\'s models instead of options and a free-text answer', () => {
		render(UserQuestion, { data: modelQuestion() });
		expect(screen.getByRole('heading', { name: 'Which model should subagents use?' })).toBeTruthy();
		expect(screen.getByRole('listbox', { name: 'Subagent model' })).toBeTruthy();
		expect(option('Sonnet')).toBeTruthy();
		expect(screen.queryByLabelText('Your answer')).toBeNull();
		expect(screen.queryByText('Skip this question')).toBeNull();
		expect((screen.getByRole('button', { name: 'Use this model' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText('Choose a model above')).toBeTruthy();
	});

	it('answers with the chosen model id', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: modelQuestion(), onRespond });
		await fireEvent.click(option('Sonnet'));
		expect(screen.getByText('Sonnet', { selector: '.picked strong' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Use this model' }));
		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'subagent-default-1', tool_use_id: 'subagent-default-1',
			answers: { 'Which model should subagents use?': 'sonnet' }, skipped: []
		});
	});

	it('"Not now" dismisses the question', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: modelQuestion(), onRespond });
		await fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
		expect(onRespond).toHaveBeenCalledWith({
			request_id: 'subagent-default-1', tool_use_id: 'subagent-default-1', answers: {}, dismissed: true
		});
	});

	it('cannot be answered while disconnected', async () => {
		const onRespond = vi.fn(); render(UserQuestion, { data: modelQuestion(), onRespond, disabled: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
		expect(onRespond).not.toHaveBeenCalled();
	});
});
