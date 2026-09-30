/**
 * Behavior tests for ActiveTasks — the collapsible tail line for background
 * tasks: a spinner + "N running tasks" collapsed line that expands to a |__
 * tree of each task with an "x" to stop it.
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
		toolUseId: null,
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

	it('shows the "N running tasks" line with a spinner and a row per task (expanded default)', () => {
		const tasks = [
			makeTask({ id: 'a', subject: 'Task A' }),
			makeTask({ id: 'b', subject: 'Task B', kind: 'shell' })
		];
		const { container } = render(ActiveTasks, { props: { tasks } });

		expect(screen.getByText('2 running tasks')).toBeTruthy();
		expect(container.querySelector('.spinner')).not.toBeNull();
		expect(screen.getByText('Task A')).toBeTruthy();
		expect(screen.getByText('Task B')).toBeTruthy();
	});

	it('pluralizes the label (1 task vs N tasks)', () => {
		render(ActiveTasks, { props: { tasks: [makeTask()] } });
		expect(screen.getByText('1 running task')).toBeTruthy();
	});

	it('draws a |__ tree branch per task row', () => {
		const { container } = render(ActiveTasks, {
			props: { tasks: [makeTask({ subject: 'A' }), makeTask({ id: 'b', subject: 'B' })] }
		});
		expect(container.querySelectorAll('.tree-branch').length).toBe(2);
	});

	it('shows a kind badge per task — agent vs shell', () => {
		render(ActiveTasks, {
			props: { tasks: [makeTask({ kind: 'agent' }), makeTask({ id: 'b', kind: 'shell' })] }
		});
		expect(screen.getByText('agent')).toBeTruthy();
		expect(screen.getByText('shell')).toBeTruthy();
	});

	it('collapses to just the tail line and hides task rows', async () => {
		const { container } = render(ActiveTasks, {
			props: { tasks: [makeTask({ id: 'a', subject: 'Task A' })] }
		});

		await fireEvent.click(screen.getByRole('button', { name: /1 running task/i }));

		expect(screen.getByText('1 running task')).toBeTruthy();
		expect(screen.queryByText('Task A')).toBeNull();
		expect(container.querySelector('.task-tree')).toBeNull();
	});

	it('expands again from the collapsed state on a second click', async () => {
		render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });

		await fireEvent.click(screen.getByRole('button', { name: /1 running task/i }));
		expect(screen.queryByText('Task A')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /1 running task/i }));
		expect(screen.getByText('Task A')).toBeTruthy();
	});

	it('renders a stop button per running task when onStop is provided and calls it with the task id', async () => {
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

	it('re-enables the stop button when the stop fails (onStop resolves false)', async () => {
		// A stop that did not take (ghost task / dead session) resolves `false`.
		// The button must re-enable so the user can retry.
		const onStop = vi.fn(async () => false);
		render(ActiveTasks, { props: { tasks: [makeTask()], onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i }) as HTMLButtonElement;
		await fireEvent.click(stopBtn);

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(stopBtn.disabled).toBe(false);
		await fireEvent.click(stopBtn);
		expect(onStop).toHaveBeenCalledTimes(2);
	});

	it('re-enables the stop button when onStop throws', async () => {
		const onStop = vi.fn(async () => {
			throw new Error('network');
		});
		render(ActiveTasks, { props: { tasks: [makeTask()], onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i }) as HTMLButtonElement;
		await fireEvent.click(stopBtn);

		expect(stopBtn.disabled).toBe(false);
	});

	it('keeps the stop button disabled on a successful stop (resolves undefined)', async () => {
		// A successful stop leaves the button disabled + dimmed; the next /live
		// poll drops the whole row once the registry marks the task terminal.
		const onStop = vi.fn(async () => undefined);
		render(ActiveTasks, { props: { tasks: [makeTask()], onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i }) as HTMLButtonElement;
		await fireEvent.click(stopBtn);

		expect(stopBtn.disabled).toBe(true);
	});

	// ── Terminal (failed/killed) tasks ────────────────────────────────────────
	// A background task whose client died is reaped to killed/error and surfaced
	// for a retention window so the tree shows the failure instead of silently
	// vanishing. Terminal receipts remain in history; they do not occupy the live task strip.
	describe('active-only task surfacing', () => {
		it.each(['killed', 'error', 'completed'] as const)('hides %s tasks from the live UI', (status) => {
			const { container } = render(ActiveTasks, { tasks: [makeTask({ status })] });
			expect(container.querySelector('.active-tasks')).toBeNull();
		});
		it('keeps only the running task and its stop control in a mixed list', () => {
			const { container } = render(ActiveTasks, { tasks: [
				makeTask({ id: 'dead', subject: 'Crashed', status: 'killed' }),
				makeTask({ id: 'live', subject: 'Still going', status: 'running' })
			], onStop: vi.fn() });
			expect(screen.getByText('Still going')).toBeTruthy();
			expect(screen.queryByText('Crashed')).toBeNull();
			expect(container.querySelectorAll('.task-row')).toHaveLength(1);
			expect(screen.getAllByRole('button', { name: /stop task/i })).toHaveLength(1);
		});
		it('removes a task as soon as a live update makes it terminal', async () => {
			const view = render(ActiveTasks, { tasks: [makeTask({ id: 'live', subject: 'Live task' })] });
			expect(screen.getByText('Live task')).toBeTruthy();
			await view.rerender({ tasks: [makeTask({ id: 'live', subject: 'Live task', status: 'completed' })] });
			expect(view.container.querySelector('.active-tasks')).toBeNull();
		});
	});

	// ── Agent display names ──────────────────────────────────────────────────
	describe('agent display names', () => {
		it('shows the dispatched agent name instead of the generic badge', () => {
			render(ActiveTasks, {
				props: { tasks: [makeTask({ agent: 'Explore' })] }
			});
			expect(screen.getByText('Explore')).toBeTruthy();
			expect(screen.queryByText('agent')).toBeNull();
		});
	});
});
