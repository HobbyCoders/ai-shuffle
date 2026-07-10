/**
 * Behavior tests for StopButton — the informed-choice Stop control.
 *
 * The `onStop` callback now carries a `stopBackground` boolean:
 *   • Flag OFF (preserveBackground=false — legacy): the SDK interrupt is
 *     query-scoped and cancels every in-flight background Task child too, so Stop
 *     is a FULL stop. With running agents a confirm warns of the consequence; with
 *     none it's one click. Either way we call onStop(true) — the backend ignores
 *     the field when the flag is off, so semantics are exactly as before.
 *   • Flag ON (preserveBackground=true — Claude Code's Esc): Stop keeps background
 *     agents running by DEFAULT and stops only the foreground turn. With running
 *     agents the confirm's primary action calls onStop(false); an opt-in checkbox
 *     escalates to onStop(true). With no agents it's an immediate onStop(false).
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
		toolUseId: null,
		startedAt: new Date(Date.now() - 5000).toISOString(),
		updatedAt: new Date().toISOString(),
		...over
	};
}

afterEach(() => cleanup());

function stopButton() {
	return screen.getByRole('button', { name: /stop generating/i });
}

describe('StopButton · flag OFF (legacy full-stop)', () => {
	it('(a) with active background tasks, Stop shows the confirm and does NOT interrupt yet', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask({ id: 'a' }), makeTask({ id: 'b' })], onStop }
		});

		await fireEvent.click(stopButton());

		// The interrupt is withheld until the user confirms.
		expect(onStop).not.toHaveBeenCalled();
		// The confirm layer explains the consequence and counts the running agents.
		expect(screen.getByRole('dialog')).toBeTruthy();
		expect(screen.getByText(/2 background agents are still running/i)).toBeTruthy();
		expect(screen.getByRole('button', { name: /stop everything/i })).toBeTruthy();
	});

	it('(b) confirming sends a FULL stop — onStop(true) once — and closes the dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [makeTask()], onStop } });

		await fireEvent.click(stopButton());
		await fireEvent.click(screen.getByRole('button', { name: /stop everything/i }));

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith(true);
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

	it('(d) with zero active tasks, Stop interrupts immediately with onStop(true) and no dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, { props: { activeTasks: [], onStop } });

		await fireEvent.click(stopButton());

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith(true);
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
		expect(onStop).toHaveBeenCalledWith(true);
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

describe('StopButton · flag ON (preserve background by default)', () => {
	it('with running agents, the confirm explains they keep running and offers an opt-in', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: {
				activeTasks: [makeTask({ id: 'a' }), makeTask({ id: 'b' })],
				preserveBackground: true,
				onStop
			}
		});

		await fireEvent.click(stopButton());

		expect(onStop).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog')).toBeTruthy();
		expect(screen.getByText(/2 background agents will keep running/i)).toBeTruthy();
		// The opt-in control is present but unchecked by default.
		const optIn = screen.getByLabelText(/also stop background agents/i) as HTMLInputElement;
		expect(optIn.checked).toBe(false);
		expect(screen.getByRole('button', { name: /stop response/i })).toBeTruthy();
	});

	it('the primary confirm action stops the foreground only — onStop(false)', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask()], preserveBackground: true, onStop }
		});

		await fireEvent.click(stopButton());
		await fireEvent.click(screen.getByRole('button', { name: /stop response/i }));

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith(false);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('opting in (checking the box) then confirming also stops agents — onStop(true)', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask()], preserveBackground: true, onStop }
		});

		await fireEvent.click(stopButton());
		await fireEvent.click(screen.getByLabelText(/also stop background agents/i));
		await fireEvent.click(screen.getByRole('button', { name: /stop response/i }));

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith(true);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('with no running agents, Stop is immediate and foreground-only — onStop(false), no dialog', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [], preserveBackground: true, onStop }
		});

		await fireEvent.click(stopButton());

		expect(onStop).toHaveBeenCalledTimes(1);
		expect(onStop).toHaveBeenCalledWith(false);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('re-opening the confirm resets the opt-in so a prior tick never leaks', async () => {
		const onStop = vi.fn();
		render(StopButton, {
			props: { activeTasks: [makeTask()], preserveBackground: true, onStop }
		});

		// Open, tick the box, cancel.
		await fireEvent.click(stopButton());
		await fireEvent.click(screen.getByLabelText(/also stop background agents/i));
		const cancel = screen
			.getAllByRole('button', { name: /^cancel$/i })
			.find((b) => b.textContent?.trim() === 'Cancel');
		await fireEvent.click(cancel!);

		// Re-open — the box must be back to unchecked.
		await fireEvent.click(stopButton());
		const optIn = screen.getByLabelText(/also stop background agents/i) as HTMLInputElement;
		expect(optIn.checked).toBe(false);
	});
});
