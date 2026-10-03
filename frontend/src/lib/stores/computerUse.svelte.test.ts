/**
 * Computer-use status poller: one poller per app instance, 2 s while the
 * capability is available, 60 s once the server says it is unavailable, and
 * fully paused while the page is hidden.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const getStatus = vi.fn();
const setEnabled = vi.fn();
const stopControl = vi.fn();
const setInputMode = vi.fn();
vi.mock('$lib/api/computerUse', () => ({
	getComputerUseStatus: (...args: unknown[]) => getStatus(...args),
	setComputerUseEnabled: (...args: unknown[]) => setEnabled(...args),
	setComputerUseInputMode: (...args: unknown[]) => setInputMode(...args),
	stopComputerUse: (...args: unknown[]) => stopControl(...args)
}));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

import { computerUse, POLL_AVAILABLE_MS, POLL_UNAVAILABLE_MS } from './computerUse.svelte';

function status(available: boolean, enabled = false) {
	return {
		available,
		reason: available ? 'ok' : 'no display',
		enabled,
		active: false,
		display: null,
		display_label: null
	};
}

let visibility: DocumentVisibilityState = 'visible';

// Drain microtasks so an awaited refresh() settles under fake timers.
const tick = async (n = 10) => {
	for (let i = 0; i < n; i++) await Promise.resolve();
};

beforeEach(() => {
	vi.useFakeTimers();
	getStatus.mockReset();
	setEnabled.mockReset();
	setInputMode.mockReset();
	stopControl.mockReset();
	visibility = 'visible';
	Object.defineProperty(document, 'visibilityState', {
		configurable: true,
		get: () => visibility
	});
});

describe('computerUse emergency stop', () => {
	it('updates display and cursor from the same status snapshot', async () => {
		getStatus.mockResolvedValue({ ...status(true, true), active: true, display: 2,
			display_label: 'Display 2 · 2560×1600', cursor: [-100, 200], action: 'left_click',
			geometry: { left: -2560, top: -200, width: 2560, height: 1600 }, escape_available: true });
		await computerUse.refresh();
		expect(computerUse.display).toBe(2);
		expect(computerUse.cursor).toEqual([-100, 200]);
		expect(computerUse.escapeAvailable).toBe(true);
	});

	it('clears activity only after the server acknowledges Stop', async () => {
		computerUse.active = true;
		let finish!: (value: { stopped: number }) => void;
		stopControl.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
		const pending = computerUse.halt();
		expect(computerUse.stopping).toBe(true);
		expect(computerUse.active).toBe(true);
		await computerUse.halt();
		expect(stopControl).toHaveBeenCalledTimes(1);
		finish({ stopped: 1 });
		await pending;
		expect(computerUse.active).toBe(false);
		expect(computerUse.stopReason).toBe('user');
		expect(computerUse.cursor).toBeNull();
	});

	it('does not let an older in-flight poll undo a successful Stop', async () => {
		let finishPoll!: (value: unknown) => void;
		getStatus.mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve; }));
		const poll = computerUse.refresh();
		stopControl.mockResolvedValue({ stopped: 1 });
		await computerUse.halt();
		finishPoll({ ...status(true, true), active: true });
		await poll;
		expect(computerUse.active).toBe(false);
	});

	it('keeps control visible if Stop fails', async () => {
		computerUse.active = true;
		stopControl.mockRejectedValue(new Error('offline'));
		await computerUse.halt();
		expect(computerUse.active).toBe(true);
		expect(computerUse.stopping).toBe(false);
	});
});

afterEach(() => {
	// Drain every subscriber a test may have left behind.
	for (let i = 0; i < 5; i++) computerUse.stop();
	vi.useRealTimers();
});

describe('computerUse poller', () => {
	it('polls every 2 s while available', async () => {
		getStatus.mockResolvedValue(status(true));
		computerUse.start();
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(3);
	});

	it('backs off to 60 s once the server reports unavailable', async () => {
		getStatus.mockResolvedValue(status(false));
		computerUse.start();
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);
		expect(computerUse.available).toBe(false);
		expect(computerUse.pollIntervalMs).toBe(POLL_UNAVAILABLE_MS);

		// Ten "available" intervals pass with no further request.
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS * 10);
		expect(getStatus).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(POLL_UNAVAILABLE_MS - POLL_AVAILABLE_MS * 10);
		expect(getStatus).toHaveBeenCalledTimes(2);

		// Comes back: the next interval is the fast one again.
		getStatus.mockResolvedValue(status(true));
		await vi.advanceTimersByTimeAsync(POLL_UNAVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(3);
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(4);
	});

	it('shares one poller across many subscribers', async () => {
		getStatus.mockResolvedValue(status(true));
		computerUse.start();
		computerUse.start();
		computerUse.start();
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(2);

		// Two of three consumers unmount: still polling.
		computerUse.stop();
		computerUse.stop();
		expect(computerUse.polling).toBe(true);
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(3);

		// Last one unmounts: silence.
		computerUse.stop();
		expect(computerUse.polling).toBe(false);
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS * 5);
		expect(getStatus).toHaveBeenCalledTimes(3);
	});

	it('stops entirely while the page is hidden and resumes on visible', async () => {
		getStatus.mockResolvedValue(status(true));
		computerUse.start();
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);

		visibility = 'hidden';
		document.dispatchEvent(new Event('visibilitychange'));
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS * 10);
		expect(getStatus).toHaveBeenCalledTimes(1);

		visibility = 'visible';
		document.dispatchEvent(new Event('visibilitychange'));
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(3);
	});

	it('does not start polling in a hidden page until it is shown', async () => {
		getStatus.mockResolvedValue(status(true));
		visibility = 'hidden';
		computerUse.start();
		await tick();
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS * 3);
		expect(getStatus).not.toHaveBeenCalled();

		visibility = 'visible';
		document.dispatchEvent(new Event('visibilitychange'));
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);
	});

	it('never stacks requests when the backend is slow', async () => {
		let resolveFirst: ((v: unknown) => void) | null = null;
		getStatus.mockImplementationOnce(
			() =>
				new Promise((resolve) => {
					resolveFirst = resolve;
				})
		);
		getStatus.mockResolvedValue(status(true));
		computerUse.start();
		await tick();
		expect(getStatus).toHaveBeenCalledTimes(1);

		// The first request is still pending: no timer is armed until it
		// settles, and an explicit refresh() is a no-op meanwhile.
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS * 3);
		await computerUse.refresh();
		expect(getStatus).toHaveBeenCalledTimes(1);

		resolveFirst!(status(true));
		await tick();
		await vi.advanceTimersByTimeAsync(POLL_AVAILABLE_MS);
		expect(getStatus).toHaveBeenCalledTimes(2);
	});
});

describe('computerUse toggle', () => {
	it('flips optimistically, confirms from the server, and refreshes', async () => {
		getStatus.mockResolvedValue(status(true, false));
		setEnabled.mockResolvedValue({ enabled: true });
		computerUse.start();
		await tick();
		expect(computerUse.enabled).toBe(false);

		const flip = computerUse.setEnabled(true);
		expect(computerUse.enabled).toBe(true); // optimistic
		// The refresh inside setEnabled reads the NEW state back.
		getStatus.mockResolvedValue(status(true, true));
		await flip;
		expect(setEnabled).toHaveBeenCalledWith(true);
		expect(computerUse.enabled).toBe(true);
	});

	it('rolls back when the server rejects the toggle', async () => {
		getStatus.mockResolvedValue(status(true, false));
		setEnabled.mockRejectedValue(new Error('forbidden'));
		computerUse.start();
		await tick();

		await computerUse.setEnabled(true);
		expect(computerUse.enabled).toBe(false);
	});
});

describe('computerUse input mode', () => {
	it('reads the mode from status and treats anything unknown as shared', async () => {
		getStatus.mockResolvedValue({ ...status(true), input_mode: 'dedicated_desktop' });
		await computerUse.refresh();
		expect(computerUse.inputMode).toBe('dedicated_desktop');
		getStatus.mockResolvedValue({ ...status(true), input_mode: 'something_new' });
		await computerUse.refresh();
		expect(computerUse.inputMode).toBe('shared_desktop');
		getStatus.mockResolvedValue(status(true));
		await computerUse.refresh();
		expect(computerUse.inputMode).toBe('shared_desktop');
	});

	it('switches optimistically and keeps the server answer', async () => {
		getStatus.mockResolvedValue(status(true));
		await computerUse.refresh();
		setInputMode.mockResolvedValue({ input_mode: 'dedicated_desktop' });
		const change = computerUse.setInputMode('dedicated_desktop');
		expect(computerUse.inputMode).toBe('dedicated_desktop');
		getStatus.mockResolvedValue({ ...status(true), input_mode: 'dedicated_desktop' });
		await change;
		expect(setInputMode).toHaveBeenCalledWith('dedicated_desktop');
		expect(computerUse.inputMode).toBe('dedicated_desktop');
	});

	it('rolls back when the server rejects the change', async () => {
		getStatus.mockResolvedValue(status(true));
		await computerUse.refresh();
		setInputMode.mockRejectedValue(new Error('forbidden'));
		await computerUse.setInputMode('dedicated_desktop');
		expect(computerUse.inputMode).toBe('shared_desktop');
	});

	it('ignores an in-flight poll that still carries the old mode', async () => {
		getStatus.mockResolvedValue(status(true));
		await computerUse.refresh();
		let finishPoll!: (value: unknown) => void;
		getStatus.mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve; }));
		const poll = computerUse.refresh();
		setInputMode.mockResolvedValue({ input_mode: 'dedicated_desktop' });
		await computerUse.setInputMode('dedicated_desktop');
		finishPoll(status(true));
		await poll;
		expect(computerUse.inputMode).toBe('dedicated_desktop');
	});
});
