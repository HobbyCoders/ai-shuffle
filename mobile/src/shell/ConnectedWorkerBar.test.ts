import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Install } from '$lib/api/installs';

vi.mock('$lib/api/installs', () => ({ listInstalls: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => ({ openSettings: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { sessions: writable([]) };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { streamingSessions: writable(new Map()) };
});
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

import { listInstalls } from '$lib/api/installs';
import { openSettings } from '$lib/settings/navigation.svelte';
import ConnectedWorkerBar from './ConnectedWorkerBar.svelte';
import * as workerConnection from './worker-connection';

type TestWindow = Window & { shuffleShell?: unknown };
const realLocation = window.location;
const postMessage = vi.fn();
const posted = () => postMessage.mock.calls.map(([raw]) => JSON.parse(raw));
const installs: Install[] = [
	{ id: 'pc', device_name: 'DesktopWorker', base_url: 'https://pc.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: true },
	{ id: 'nas', device_name: 'ServerWorker', base_url: 'https://nas.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: false }
];
const reply = () => ({ installs: installs.map((worker) => ({ ...worker })), self_instance_id: 'pc' });
const chip = () => screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' });

function inApp(capabilities: string[], bridge = true) {
	window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://pc.test', token: 'shc_test', native: { platform: 'android', bridge: 1, capabilities } };
	if (bridge) (window as TestWindow).shuffleShell = { postMessage };
}

beforeEach(() => {
	postMessage.mockReset();
	vi.mocked(openSettings).mockReset();
	vi.mocked(listInstalls).mockReset().mockResolvedValue(reply());
});

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.restoreAllMocks();
	Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
	delete window.__SHUFFLE_MOBILE__;
	delete (window as TestWindow).shuffleShell;
});

describe('ConnectedWorkerBar in the Android app', () => {
	it('tells the app it shows its own bar, and takes that back when it goes', async () => {
		inApp(['worker-switch']);
		const { unmount } = render(ConnectedWorkerBar);
		await chip();
		expect(posted()).toEqual([{ type: 'worker-bar', shown: true }]);
		unmount();
		expect(posted()).toEqual([{ type: 'worker-bar', shown: true }, { type: 'worker-bar', shown: false }]);
	});

	it('opens Settings > Devices, not the native Workers screen, while connected', async () => {
		inApp(['worker-switch']);
		render(ConnectedWorkerBar);
		await fireEvent.click(await chip());
		await fireEvent.click(screen.getByRole('button', { name: 'Manage workers' }));
		expect(openSettings).toHaveBeenCalledWith({ category: 'devices' });
		expect(posted()).not.toContainEqual({ type: 'workers' });
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('keeps native pairing available when the worker cannot be reached', async () => {
		inApp(['worker-switch']);
		vi.mocked(listInstalls).mockRejectedValue(new TypeError('Failed to fetch'));
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Connection to this worker lost. Switch worker' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Open app Workers' }));
		expect(posted().at(-1)).toEqual({ type: 'workers' });
		expect(openSettings).not.toHaveBeenCalled();
	});

	it('gives the bar back when the app does not switch', async () => {
		inApp(['worker-switch']);
		const assign = vi.fn();
		Object.defineProperty(window, 'location', { configurable: true, value: { href: 'https://pc.test/mobile/', origin: 'https://pc.test', assign } });
		render(ConnectedWorkerBar);
		await fireEvent.click(await chip());
		await screen.findByRole('radio', { name: /ServerWorker/ });
		vi.useFakeTimers();
		await fireEvent.click(screen.getByRole('radio', { name: /ServerWorker/ }));
		expect(assign).toHaveBeenCalledWith(expect.stringContaining('?connect=https%3A%2F%2Fnas.test'));
		expect(screen.getByRole('button', { name: 'Connecting to ServerWorker' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Manage workers' }).hasAttribute('disabled')).toBe(true);
		await vi.advanceTimersByTimeAsync(5000);
		expect(screen.getByRole('button', { name: 'Connected to DesktopWorker. Switch worker' })).toBeTruthy();
	});
});

describe('ConnectedWorkerBar without the Android bridge', () => {
	it('still offers Settings > Devices to add and manage workers', async () => {
		inApp(['worker-switch'], false);
		render(ConnectedWorkerBar);
		await fireEvent.click(await chip());
		await fireEvent.click(screen.getByRole('button', { name: 'Manage workers' }));
		expect(openSettings).toHaveBeenCalledWith({ category: 'devices' });
		expect(postMessage).not.toHaveBeenCalled();
	});
});

describe('ConnectedWorkerBar status', () => {
	it('shows just the dot and the worker name on the bar, and the full sentence to assistive tech', async () => {
		inApp(['worker-switch']);
		render(ConnectedWorkerBar);
		const button = await chip();
		expect(button.classList.contains('conn-chip')).toBe(true);
		expect(button.textContent?.trim()).toBe('DesktopWorker');
		expect(button.querySelector('.dot:not(.off)')).toBeTruthy();
		expect(button.getAttribute('aria-haspopup')).toBe('dialog');
	});

	it('renders an offline current worker supplied by the connection model', async () => {
		vi.spyOn(workerConnection, 'connectionWorkers').mockReturnValue([
			{ id: 'pc', name: 'DesktopWorker', online: false, current: true, running: 0, baseUrl: 'https://pc.test' }
		]);
		render(ConnectedWorkerBar);
		const button = await screen.findByRole('button', { name: 'DesktopWorker is offline. Switch worker' });
		expect(button.querySelector('.dot.off')).toBeTruthy();
		await fireEvent.click(button);
		expect(screen.getByRole('radio', { name: /DesktopWorker Offline/ })).toBeTruthy();
	});

	it('uses the fresh current-worker request instead of an old offline registry heartbeat', async () => {
		const workers = reply();
		workers.installs[0].online = false;
		vi.mocked(listInstalls).mockResolvedValue(workers);
		render(ConnectedWorkerBar);
		expect((await chip()).querySelector('.dot:not(.off)')).toBeTruthy();
	});

	it('does not claim to be connected before the first status request finishes', async () => {
		let resolve!: (value: ReturnType<typeof reply>) => void;
		vi.mocked(listInstalls).mockReturnValue(new Promise((done) => { resolve = done; }));
		render(ConnectedWorkerBar);
		const button = screen.getByRole('button', { name: 'Checking worker connection. Switch worker' });
		expect(button.querySelector('.conn-spin')).toBeTruthy();
		expect(button.querySelector('.dot:not(.off)')).toBeNull();
		await fireEvent.click(button);
		expect(screen.getByRole('status').textContent).toContain('Loading workers');
		resolve(reply());
		await chip();
	});

	it('refreshes a visible page, marks a failed connection offline, then recovers', async () => {
		vi.useFakeTimers();
		render(ConnectedWorkerBar);
		await vi.advanceTimersByTimeAsync(0);
		vi.mocked(listInstalls).mockRejectedValueOnce(new TypeError('Failed to fetch'));
		await vi.advanceTimersByTimeAsync(15000);
		const button = screen.getByRole('button', { name: 'Connection to DesktopWorker lost. Switch worker' });
		expect(button.querySelector('.dot.off')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(15000);
		expect(screen.getByRole('button', { name: 'Connected to DesktopWorker. Switch worker' })).toBeTruthy();
	});

	it('aborts a hung status refresh, shows the lost connection, and retries later', async () => {
		vi.useFakeTimers();
		render(ConnectedWorkerBar);
		await vi.advanceTimersByTimeAsync(0);
		const signals: AbortSignal[] = [];
		vi.mocked(listInstalls).mockImplementationOnce((options) => new Promise((_, reject) => {
			const signal = options!.signal!;
			signals.push(signal);
			signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
		}));
		await vi.advanceTimersByTimeAsync(15000);
		expect(signals[0].aborted).toBe(false);
		await vi.advanceTimersByTimeAsync(5000);
		expect(signals[0].aborted).toBe(true);
		const button = screen.getByRole('button', { name: 'Connection to DesktopWorker lost. Switch worker' });
		expect(button.querySelector('.dot.off')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(10000);
		expect(screen.getByRole('button', { name: 'Connected to DesktopWorker. Switch worker' })).toBeTruthy();
		expect(listInstalls).toHaveBeenCalledTimes(3);
	});

	it('marks a browser offline immediately and refreshes when it comes back online', async () => {
		render(ConnectedWorkerBar);
		await chip();
		await fireEvent(window, new Event('offline'));
		expect(screen.getByRole('button', { name: 'Connection to DesktopWorker lost. Switch worker' })).toBeTruthy();
		await fireEvent(window, new Event('online'));
		await chip();
		expect(listInstalls).toHaveBeenCalledTimes(2);
	});

	it('does not poll while the page is hidden and refreshes when visible again', async () => {
		vi.useFakeTimers();
		render(ConnectedWorkerBar);
		await vi.advanceTimersByTimeAsync(0);
		const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
		await vi.advanceTimersByTimeAsync(30000);
		expect(listInstalls).toHaveBeenCalledTimes(1);
		visibility.mockReturnValue('visible');
		await fireEvent(document, new Event('visibilitychange'));
		expect(listInstalls).toHaveBeenCalledTimes(2);
		visibility.mockRestore();
	});

	it('cancels a pending status request when the chip unmounts', async () => {
		const signals: AbortSignal[] = [];
		vi.mocked(listInstalls).mockImplementationOnce((options) => new Promise((_, reject) => {
			const signal = options!.signal!;
			signals.push(signal);
			signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
		}));
		const { unmount } = render(ConnectedWorkerBar);
		await tick();
		expect(signals[0].aborted).toBe(false);
		unmount();
		expect(signals[0].aborted).toBe(true);
	});

	it('cleans up its refresh timer and browser listeners when unmounted', async () => {
		vi.useFakeTimers();
		const { unmount } = render(ConnectedWorkerBar);
		await tick();
		await Promise.resolve();
		unmount();
		const calls = vi.mocked(listInstalls).mock.calls.length;
		await vi.advanceTimersByTimeAsync(30000);
		await fireEvent(window, new Event('online'));
		expect(listInstalls).toHaveBeenCalledTimes(calls);
	});
});

describe('ConnectedWorkerBar sheet', () => {
	it('explains the phone connection without confusing it with Runs on', async () => {
		render(ConnectedWorkerBar);
		await fireEvent.click(await chip());
		expect(screen.getByText('The worker this phone connects through. Pick one to switch now.')).toBeTruthy();
		expect(screen.getByText('Settings > Devices')).toBeTruthy();
		expect(screen.getByText('To move one chat, use Runs on in Chat controls.')).toBeTruthy();
	});

	it('shows an empty state that still lets the phone add workers', async () => {
		vi.mocked(listInstalls).mockResolvedValue({ installs: [], self_instance_id: 'pc' });
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Worker connection unknown. Switch worker' }));
		expect(screen.getByText('No workers found. Add a worker in Settings > Devices.')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Manage workers' })).toBeTruthy();
	});

	it('shows an error and a working retry rather than a green connection dot', async () => {
		vi.mocked(listInstalls).mockRejectedValue(new TypeError('Failed to fetch'));
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Connection to this worker lost. Switch worker' }));
		expect(screen.getByRole('alert').textContent).toContain('Could not refresh worker status.');
		await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' }).hasAttribute('disabled')).toBe(false));
		vi.mocked(listInstalls).mockResolvedValue(reply());
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		await chip();
		expect(screen.queryByRole('alert')).toBeNull();
		expect(screen.getByRole('radio', { name: /DesktopWorker Online/ })).toBeTruthy();
	});

	it('keeps keyboard focus inside the sheet and returns it to the chip on Escape', async () => {
		render(ConnectedWorkerBar);
		const button = await chip();
		await fireEvent.click(button);
		const manage = screen.getByRole('button', { name: 'Manage workers' });
		manage.focus();
		await fireEvent.keyDown(manage, { key: 'Tab' });
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(button);
	});
});
