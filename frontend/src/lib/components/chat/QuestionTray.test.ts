import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import QuestionTray from './QuestionTray.svelte';
import type { UserQuestionData } from '../UserQuestion.svelte';

const request = (id = 'one'): UserQuestionData => ({ request_id: id, tool_use_id: id, nonblocking: true, questions: [{ question: `Choose ${id}?`, header: id, options: [], multiSelect: false }] });
afterEach(cleanup);
describe('QuestionTray', () => {
	it('does not take focus on arrival and never renders a dialog', () => {
		const composer = document.createElement('textarea'); document.body.append(composer); composer.focus();
		render(QuestionTray, { requests: [request()], working: true, onRespond: vi.fn() });
		expect(document.activeElement).toBe(composer); expect(screen.queryByRole('dialog')).toBeNull(); composer.remove();
		expect(screen.getByText('Still working')).toBeTruthy();
	});
	it('collapses without sending or discarding a draft', async () => {
		const onRespond = vi.fn(); render(QuestionTray, { requests: [request()], onRespond });
		await fireEvent.input(screen.getByLabelText('Your answer'), { target: { value: 'A draft' } });
		const toggle = screen.getByRole('button', { name: /Questions for you/ });
		await fireEvent.click(toggle); expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByRole('textbox')).toBeNull(); expect(onRespond).not.toHaveBeenCalled();
		await fireEvent.click(toggle); expect((screen.getByLabelText('Your answer') as HTMLTextAreaElement).value).toBe('A draft');
	});
	it('retains drafts when switching requests and receiving new objects', async () => {
		const requests = [request('one'), request('two')]; const view = render(QuestionTray, { requests, onRespond: vi.fn() });
		await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'First draft' } });
		await fireEvent.click(screen.getByRole('button', { name: 'two' }));
		await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'Second draft' } });
		await view.rerender({ requests: requests.map(r => ({ ...r })), onRespond: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'one' }));
		expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('First draft');
	});
	it('moves to the remaining question after acknowledgement removes the selected request', async () => {
		const onRespond = vi.fn(); const view = render(QuestionTray, { requests: [request('one'), request('two')], onRespond });
		await view.rerender({ requests: [request('two')], onRespond });
		expect(screen.getByText('Choose two?')).toBeTruthy();
	});
	it('disables answers while disconnected and distinguishes idle from working', async () => {
		const view = render(QuestionTray, { requests: [request()], connected: false, working: true, onRespond: vi.fn() });
		expect(screen.getByText('Reconnect to answer')).toBeTruthy();
		expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
		await view.rerender({ requests: [request()], connected: true, working: false, onRespond: vi.fn() });
		expect(screen.getByText('Answer to continue')).toBeTruthy();
	});
});
