/**
 * Behavior tests for SystemMessage's background-task rows (task_updated /
 * task_notification). These subtypes render a collapsed header matching the
 * tool-card convention: three-way status icon -> bot icon -> "Task" label ->
 * status chip -> description summary.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/svelte';
import SystemMessage from './SystemMessage.svelte';
import type { ChatMessage } from '$lib/stores/types';

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
