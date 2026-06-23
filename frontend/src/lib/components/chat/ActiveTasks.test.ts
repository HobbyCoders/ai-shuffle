/**
 * Behavior tests for ActiveTasks — the live background-tasks strip.
 *
 * Covers the additions: the collapse/minimize toggle (+ its "Running N tasks"
 * line and persistence), per-task stop buttons, and the empty-state guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import ActiveTasks from './ActiveTasks.svelte';
import type { Task } from '$lib/stores/types';

function makeTask(over: Partial<Task> = {}): Task {
	return {
		id: 'task-1',
		subject: 'Refactor the widget',
		status: 'running',
		kind: 'agent',
		summary: null,
		startedAt: new Date(Date.now() - 5000).toISOString(),
		updatedAt: new Date().toISOString(),
		...over
	};
}

beforeEach(() => {
	localStorage.clear();
});

afterEach(() => {
	cleanup();
});

describe('ActiveTasks', () => {
	it('renders nothing when there are no tasks', () => {
		const { container } = render(ActiveTasks, { props: { tasks: [] } });
		expect(container.querySelector('.active-tasks')).toBeNull();
	});

	it('shows the expanded header and a row per task', () => {
		const tasks = [
			makeTask({ id: 'a', subject: 'Task A' }),
			makeTask({ id: 'b', subject: 'Task B', kind: 'shell' })
		];
		render(ActiveTasks, { props: { tasks } });

		expect(screen.getByText('Background (2)')).toBeTruthy();
		expect(screen.getByText('Task A')).toBeTruthy();
		expect(screen.getByText('Task B')).toBeTruthy();
	});

	it('renders a stop button per task when onStop is provided and calls it with the task id', async () => {
		const onStop = vi.fn();
		const tasks = [makeTask({ id: 'kill-me', subject: 'Doomed task' })];
		render(ActiveTasks, { props: { tasks, onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i });
		await fireEvent.click(stopBtn);

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith('kill-me');
	});

	it('omits stop buttons when no onStop handler is given', () => {
		render(ActiveTasks, { props: { tasks: [makeTask()] } });
		expect(screen.queryByRole('button', { name: /stop task/i })).toBeNull();
	});

	it('disables the stop button after it is clicked (stopping state)', async () => {
		// onStop never resolves, so the row stays in the "stopping" state.
		const onStop = vi.fn(() => new Promise<void>(() => {}));
		render(ActiveTasks, { props: { tasks: [makeTask()], onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i }) as HTMLButtonElement;
		await fireEvent.click(stopBtn);

		expect(stopBtn.disabled).toBe(true);
	});

	it('collapses to a single "Running N tasks" line and hides task rows', async () => {
		const tasks = [makeTask({ id: 'a', subject: 'Task A' }), makeTask({ id: 'b', subject: 'Task B' })];
		render(ActiveTasks, { props: { tasks } });

		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));

		expect(screen.getByText('Running 2 tasks')).toBeTruthy();
		expect(screen.queryByText('Task A')).toBeNull();
		expect(screen.queryByText('Background (2)')).toBeNull();
	});

	it('pluralizes the collapsed label (1 task vs N tasks)', async () => {
		render(ActiveTasks, { props: { tasks: [makeTask()] } });
		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));
		expect(screen.getByText('Running 1 task')).toBeTruthy();
	});

	it('toggles back to expanded from the collapsed state', async () => {
		render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });

		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));
		expect(screen.queryByText('Task A')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /expand background tasks/i }));
		expect(screen.getByText('Task A')).toBeTruthy();
	});

	it('persists the collapsed preference to localStorage', async () => {
		render(ActiveTasks, { props: { tasks: [makeTask()] } });
		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));
		expect(localStorage.getItem('aihub.activeTasks.collapsed')).toBe('1');
	});

	it('starts collapsed when the persisted preference says so', () => {
		localStorage.setItem('aihub.activeTasks.collapsed', '1');
		render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });

		expect(screen.getByText('Running 1 task')).toBeTruthy();
		expect(screen.queryByText('Task A')).toBeNull();
	});
});
