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
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

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
	// The collapse state lives in the shared chat-preferences singleton; reset it
	// so persistence from one test doesn't leak into the next.
	chatPreferences.reset();
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

	it('re-enables the stop button when the stop fails (onStop resolves false)', async () => {
		// Bug 4: a stop that did not take (ghost task / dead session) resolves
		// `false`. The button must re-enable so the user can retry — not stick
		// disabled forever on a row the next poll won't remove.
		const onStop = vi.fn(async () => false);
		render(ActiveTasks, { props: { tasks: [makeTask()], onStop } });

		const stopBtn = screen.getByRole('button', { name: /stop task/i }) as HTMLButtonElement;
		await fireEvent.click(stopBtn);

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(stopBtn.disabled).toBe(false);
		// A second attempt is allowed (the guard reset).
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

	it('persists the collapsed state to the chat-preferences store', async () => {
		render(ActiveTasks, { props: { tasks: [makeTask()] } });
		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));
		expect(chatPreferences.current.activeTasksCollapsed).toBe(true);
	});

	it('starts collapsed when the persisted preference says so', () => {
		chatPreferences.setActiveTasksCollapsed(true);
		render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });

		expect(screen.getByText('Running 1 task')).toBeTruthy();
		expect(screen.queryByText('Task A')).toBeNull();
	});

	it('keeps the collapsed state across a remount (persistence round-trip)', async () => {
		const { unmount } = render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });
		await fireEvent.click(screen.getByRole('button', { name: /minimize background tasks/i }));
		expect(screen.getByText('Running 1 task')).toBeTruthy();
		unmount();

		// A fresh instance (e.g. after a reload / new turn) reads the persisted state.
		render(ActiveTasks, { props: { tasks: [makeTask({ subject: 'Task A' })] } });
		expect(screen.getByText('Running 1 task')).toBeTruthy();
		expect(screen.queryByText('Task A')).toBeNull();
	});

	// ── Terminal (failed/killed) tasks — the silent-vanish fix ────────────────
	// A background task whose SDK client died is reaped to "killed" and surfaced
	// for a retention window (see task_registry.get_visible) so the strip shows
	// the failure instead of the row disappearing with no indication.
	describe('terminal task surfacing', () => {
		it('renders a strip of only terminal tasks (the failure is still shown)', () => {
			const { container } = render(ActiveTasks, {
				props: { tasks: [makeTask({ id: 'dead', subject: 'Crashed task', status: 'killed' })] }
			});
			// The strip renders rather than collapsing to nothing.
			expect(container.querySelector('.active-tasks')).not.toBeNull();
			expect(screen.getByText('Crashed task')).toBeTruthy();
			// The failed status is visible and the row carries the killed style hook.
			expect(screen.getByText('killed')).toBeTruthy();
			expect(container.querySelector('.task-row.is-killed')).not.toBeNull();
		});

		it('marks an errored task with the is-error style and shows its status', () => {
			const { container } = render(ActiveTasks, {
				props: { tasks: [makeTask({ id: 'boom', subject: 'Errored task', status: 'error' })] }
			});
			expect(screen.getByText('error')).toBeTruthy();
			expect(container.querySelector('.task-row.is-error')).not.toBeNull();
		});

		it('omits the stop button for a terminal task even when onStop is provided', () => {
			// A dead task can't be stopped — the "x" is hidden so the only control
			// shown is for genuinely-running work.
			const onStop = vi.fn();
			render(ActiveTasks, {
				props: { tasks: [makeTask({ id: 'dead', status: 'killed' })], onStop }
			});
			expect(screen.queryByRole('button', { name: /stop task/i })).toBeNull();
		});

		it('still shows the stop button for a running task alongside a terminal one', () => {
			const onStop = vi.fn();
			render(ActiveTasks, {
				props: {
					tasks: [
						makeTask({ id: 'live', subject: 'Still going', status: 'running' }),
						makeTask({ id: 'dead', subject: 'Crashed', status: 'killed' })
					],
					onStop
				}
			});
			// Exactly one stop button — for the running task only.
			expect(screen.getAllByRole('button', { name: /stop task/i }).length).toBe(1);
		});
	});
});
