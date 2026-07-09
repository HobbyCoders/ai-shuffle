/**
 * Behavior tests for StopButton — the informed-choice Stop control.
 *
 * When the session has ACTIVE BACKGROUND TASKS, clicking Stop must NOT interrupt
 * immediately: the SDK interrupt is query-scoped and cancels every in-flight
 * background Task child too (proven — impossible to keep them at the SDK layer).
 * So Stop becomes an informed choice via a confirm layer. With no active tasks it
 * behaves exactly as before: one click, immediate stop, zero friction.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import StopButton from './StopButton.svelte';
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

afterEach(() => cleanup());

function stopButton() {
	return screen.getByRole('button', { name: /stop generating/i });
}

describe('StopButton · informed-choice Stop', () => {
	it('(a) with active background tasks, Stop shows the confirm and does NOT interrupt yet', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [makeTask({ id: 'a' }), makeTask({ id: 'b' })], onStop } });

		await fireEvent.click(stopButton());

		// The interrupt is withheld until the user confirms.
		expect(onStop).not.toHaveBeenCalled();
		// The confirm layer explains the consequence and counts the running agents.
		expect(screen.getByRole('dialog')).toBeTruthy();
		expect(screen.getByText(/2 background agents are still running/i)).toBeTruthy();
		expect(screen.getByRole('button', { name: /stop everything/i })).toBeTruthy();
	});

	it('(b) confirming sends exactly the same stop as today (onStop called once) and closes the dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [makeTask()], onStop } });

		await fireEvent.click(stopButton());
		await fireEvent.click(screen.getByRole('button', { name: /stop everything/i }));

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('(c) cancelling sends nothing and closes the dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [makeTask()], onStop } });

		await fireEvent.click(stopButton());
		// Both the visible Cancel button and the backdrop expose the "Cancel" name;
		// click the one with visible text (the footer action).
		const cancel = screen
			.getAllByRole('button', { name: /^cancel$/i })
			.find((b) => b.textContent?.trim() === 'Cancel');
		await fireEvent.click(cancel!);

		expect(onStop).not.toHaveBeenCalled();
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('(d) with zero active tasks, Stop interrupts immediately with no dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [], onStop } });

		await fireEvent.click(stopButton());

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('treats only-terminal tasks as no active work (stops immediately, no dialog)', async () => {
		// A killed/errored task can no longer be stopped, so it must not trigger the
		// informed-choice friction — Stop behaves as today.
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask({ status: 'killed' }), makeTask({ status: 'error' })], onStop }
		});

		await fireEvent.click(stopButton());

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('counts only running tasks and singularizes the copy for one agent', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask({ status: 'running' }), makeTask({ id: 'dead', status: 'killed' })], onStop }
		});

		await fireEvent.click(stopButton());

		expect(screen.getByText(/1 background agent is still running/i)).toBeTruthy();
	});

	it('portals the confirm to document.body so it escapes the input dock backdrop-filter', async () => {
		// The confirm renders inside ChatInput's `.input-wrapper`, whose
		// `backdrop-filter: blur(20px)` makes it the containing block for
		// `position: fixed` descendants — trapping/clipping the dialog inside the
		// input dock. Portaling the dialog root onto <body> escapes that ancestor.
		render(StopButton, { props: { activeTasks: [makeTask()], onStop: vi.fn() } });

		await fireEvent.click(stopButton());

		const dialog = screen.getByRole('dialog');
		expect(dialog.parentElement).toBe(document.body);
	});

	it('Escape cancels the confirm without stopping', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [makeTask()], onStop } });

		await fireEvent.click(stopButton());
		await fireEvent.keyDown(window, { key: 'Escape' });

		expect(onStop).not.toHaveBeenCalled();
		expect(screen.queryByRole('dialog')).toBeNull();
	});
});
