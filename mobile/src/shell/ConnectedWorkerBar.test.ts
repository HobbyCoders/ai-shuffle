import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

vi.mock('$lib/api/installs', () => ({
	listInstalls: vi.fn().mockResolvedValue({ installs: [
		{ id: 'pc', device_name: 'DesktopWorker', base_url: 'https://pc.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: true },
		{ id: 'nas', device_name: 'Unraid', base_url: 'https://nas.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: false }
	], self_instance_id: 'pc' })
}));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { sessions: writable([]) };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { streamingSessions: writable(new Map()) };
});
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

import ConnectedWorkerBar from './ConnectedWorkerBar.svelte';

type TestWindow = Window & { shuffleShell?: unknown };
const realLocation = window.location;
const postMessage = vi.fn();
const posted = () => postMessage.mock.calls.map(([raw]) => JSON.parse(raw));

function inApp(capabilities: string[], bridge = true) {
	window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://pc.test', token: 'shc_test', native: { platform: 'android', bridge: 1, capabilities } };
	if (bridge) (window as TestWindow).shuffleShell = { postMessage };
}

beforeEach(() => postMessage.mockReset());

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
	delete window.__SHUFFLE_MOBILE__;
	delete (window as TestWindow).shuffleShell;
});

describe('ConnectedWorkerBar in the Android app', () => {
	it('tells the app it shows its own bar, and takes that back when it goes', async () => {
		inApp(['worker-switch']);
		const { unmount } = render(ConnectedWorkerBar);
		await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' });
		expect(posted()).toEqual([{ type: 'worker-bar', shown: true }]);
		unmount();
		expect(posted()).toEqual([{ type: 'worker-bar', shown: true }, { type: 'worker-bar', shown: false }]);
	});

	it('opens the app Workers screen from the sheet', async () => {
		inApp(['worker-switch']);
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Pair, rename or remove workers' }));
		expect(posted().at(-1)).toEqual({ type: 'workers' });
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('gives the bar back when the app does not switch', async () => {
		inApp(['worker-switch']);
		const assign = vi.fn();
		Object.defineProperty(window, 'location', { configurable: true, value: { href: 'https://pc.test/mobile/', origin: 'https://pc.test', assign } });
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' }));
		await screen.findByRole('radio', { name: /Unraid/ });
		vi.useFakeTimers();
		await fireEvent.click(screen.getByRole('radio', { name: /Unraid/ }));
		expect(assign).toHaveBeenCalledWith(expect.stringContaining('?connect=https%3A%2F%2Fnas.test'));
		expect(screen.getByRole('button', { name: 'Connecting to Unraid' })).toBeTruthy();
		await vi.advanceTimersByTimeAsync(5000);
		expect(screen.getByRole('button', { name: 'Connected to DesktopWorker. Switch worker' })).toBeTruthy();
	});
});

describe('ConnectedWorkerBar elsewhere', () => {
	it('posts nothing and offers no Workers screen without the app bridge', async () => {
		inApp(['worker-switch'], false);
		render(ConnectedWorkerBar);
		await fireEvent.click(await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' }));
		expect(screen.queryByRole('button', { name: 'Pair, rename or remove workers' })).toBeNull();
		expect(postMessage).not.toHaveBeenCalled();
	});
});
