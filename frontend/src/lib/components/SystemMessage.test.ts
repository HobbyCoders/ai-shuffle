/**
 * Behavior tests for SystemMessage's background-task rows (task_updated /
 * task_notification). These subtypes render a collapsed header matching the
 * tool-card convention: three-way status icon -> bot icon -> "Task" label ->
 * status chip -> description summary.
 */
import { describe, it, expect, afterEach, vi, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import SystemMessage from './SystemMessage.svelte';
import type { ChatMessage } from '$lib/stores/types';

// The Retry button on a spent retry card posts to the resume route with the
// session the chat is currently on.
const resumeSession = vi.hoisted(() => vi.fn());
const notifyError = vi.hoisted(() => vi.fn());

vi.mock('$lib/api/sessions', () => ({ resumeSession }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: notifyError } }));
vi.mock('$lib/stores/active-session', () => ({
	activeSession: {
		subscribe: (run: (v: { sessionId: string }) => void) => {
			run({ sessionId: 'sess-active' });
			return () => {};
		}
	}
}));

function makeMessage(over: Partial<ChatMessage> = {}): ChatMessage {
	return {
		id: 'sys-1',
		role: 'system',
		content: '',
		...over
	};
}

afterEach(() => {
	cleanup();
});

describe('SystemMessage — task_updated / task_notification', () => {
	it('shows the "Task" label, a status chip, and the description summary', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'task_updated',
					systemData: {
						task_id: 'abcd1234ef',
						patch: { status: 'completed', description: 'Refactor the widget' }
					}
				})
			}
		});

		// Header name label is the literal 'Task'
		expect(screen.getByText('Task')).toBeTruthy();
		// Status chip carries the status string
		expect(screen.getByText('completed')).toBeTruthy();
		// Summary is the task description, not the old "task <id> · <status> · ..." string
		expect(screen.getByText('Refactor the widget')).toBeTruthy();
		// The collapsed summary row must not carry the old noisy concatenation.
		const summary = container.querySelector('summary');
		expect(summary?.textContent).not.toContain('abcd1234');
	});

	it('falls back to the 8-char task id when there is no description', () => {
		render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'task_notification',
					systemData: { task_id: 'abcd1234ef', patch: { status: 'stopped' } }
				})
			}
		});

		expect(screen.getByText('Task')).toBeTruthy();
		expect(screen.getByText('stopped')).toBeTruthy();
		// 8-char slice of the task id
		expect(screen.getByText('abcd1234')).toBeTruthy();
	});

	it('renders a spinner (not a check) while a task is running', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'task_updated',
					systemData: { task_id: 'run00001', patch: { status: 'running', description: 'Building' } }
				})
			}
		});

		expect(container.querySelector('.animate-spin')).not.toBeNull();
		// The green-check status icon must not be present for a running task
		expect(container.querySelector('.text-green-500')).toBeNull();
	});

	it('renders a red X for a failed task', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'task_updated',
					systemData: { task_id: 'fail0001', patch: { status: 'failed', description: 'Boom' } }
				})
			}
		});

		expect(container.querySelector('.text-red-500')).not.toBeNull();
		expect(container.querySelector('.animate-spin')).toBeNull();
	});

	it('renders a green check for a completed task', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'task_updated',
					systemData: { task_id: 'done0001', patch: { status: 'completed', description: 'Done' } }
				})
			}
		});

		expect(container.querySelector('.text-green-500')).not.toBeNull();
		expect(container.querySelector('.animate-spin')).toBeNull();
	});
});

describe('SystemMessage — provider_retry', () => {
	it('renders a counting "Retrying" row with a spinner', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'provider_retry',
					systemData: {
						attempt: 3,
						max_attempts: 10,
						reason: 'empty response from provider',
						delay_seconds: 4,
						model: 'ox-alpha'
					}
				})
			}
		});

		expect(screen.getByText('Retrying')).toBeTruthy();
		expect(container.textContent).toContain('attempt 3 of 10');
		expect(container.textContent).toContain('ox-alpha');
		// Work in flight: spinner, never a settled check or cross.
		expect(container.querySelector('.animate-spin')).not.toBeNull();
		expect(container.querySelector('.text-green-500')).toBeNull();
		expect(container.querySelector('.text-red-500')).toBeNull();
	});

	it('falls back to "?" when the attempt counters are missing', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({ systemSubtype: 'provider_retry', systemData: {} })
			}
		});

		expect(container.textContent).toContain('attempt ? of ?');
	});
});

describe('SystemMessage — provider_retry exhausted', () => {
	beforeEach(() => {
		resumeSession.mockReset();
		notifyError.mockReset();
	});

	function exhausted(over: Record<string, unknown> = {}) {
		return makeMessage({
			systemSubtype: 'provider_retry',
			systemData: {
				attempt: 10,
				max_attempts: 10,
				model: 'ox-alpha',
				exhausted: true,
				error: 'empty response, giving up',
				...over
			}
		});
	}

	it('reads as a settled failure with a Retry button', () => {
		const { container } = render(SystemMessage, { props: { message: exhausted() } });

		expect(screen.getByText('Retry failed')).toBeTruthy();
		expect(container.textContent).toContain('10 times in a row');
		expect(container.textContent).toContain('ox-alpha');
		// Settled, not in flight: the cross replaces the spinner.
		expect(container.querySelector('.animate-spin')).toBeNull();
		expect(container.querySelector('.text-red-500')).not.toBeNull();
		expect(container.querySelector('[data-testid="provider-retry-button"]')).not.toBeNull();
	});

	it('drops the button once the card has been resumed', () => {
		const { container } = render(SystemMessage, {
			props: { message: exhausted({ resumed: true }) }
		});

		expect(screen.getByText('Retry failed')).toBeTruthy();
		expect(container.textContent).toContain('resumed');
		expect(container.querySelector('[data-testid="provider-retry-button"]')).toBeNull();
	});

	it('resumes the active session and shows progress while the call is in flight', async () => {
		let release: (v: unknown) => void = () => {};
		resumeSession.mockReturnValue(new Promise((r) => { release = r; }));

		render(SystemMessage, { props: { message: exhausted() } });
		const button = screen.getByTestId('provider-retry-button') as HTMLButtonElement;

		await fireEvent.click(button);

		expect(resumeSession).toHaveBeenCalledWith('sess-active');
		expect(button.textContent?.trim()).toBe('Retrying…');
		expect(button.disabled).toBe(true);

		release({ turn_id: 't1', session_id: 'sess-active' });
	});

	it('re-enables and reports when the resume is refused', async () => {
		resumeSession.mockRejectedValue(new Error('a turn is already running'));

		render(SystemMessage, { props: { message: exhausted() } });
		const button = screen.getByTestId('provider-retry-button') as HTMLButtonElement;

		await fireEvent.click(button);
		await Promise.resolve();
		await Promise.resolve();

		expect(notifyError).toHaveBeenCalled();
		expect(notifyError.mock.calls[0][0]).toContain('a turn is already running');
	});
});

describe('SystemMessage — generic system rows', () => {
	it('renders as a flat row with a collapsed details body', () => {
		const { container } = render(SystemMessage, {
			props: {
				message: makeMessage({
					systemSubtype: 'informational',
					content: 'Something worth noting',
					systemData: { content: 'Something worth noting' }
				})
			}
		});

		// Flat-row idiom, not the old boxed card.
		expect(container.querySelector('.sys-card')).toBeNull();
		expect(container.querySelector('.sys-flat')).not.toBeNull();
		const details = container.querySelector('details');
		expect(details).not.toBeNull();
		// Collapsed by default; the body lives behind the summary row.
		expect((details as HTMLDetailsElement).open).toBe(false);
		expect(container.querySelector('summary.sys-row')).not.toBeNull();
		expect(container.querySelector('.sys-body')).not.toBeNull();
	});
});
