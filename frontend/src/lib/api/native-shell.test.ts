import { afterEach, describe, expect, it, vi } from 'vitest';
const call = vi.hoisted(() => ({ active: false, hangup: vi.fn() }));
vi.mock('$lib/services/voiceCall', async () => {
	const { writable } = await import('svelte/store');
	return {
		voiceCallState: writable({ sessionId: 's1', status: 'live' }),
		isCallActiveFor: () => call.active,
		hangup: call.hangup
	};
});

import { announceWorkerBar, hasShellBridge, nativeAppSettingsRoute, openNativeAppSettings, openNativeWorkers } from './native-shell';

type TestWindow = Window & { shuffleShell?: unknown };

function inApp(bridge?: unknown, capabilities?: string[]) {
	window.__SHUFFLE_MOBILE__ = {
		apiOrigin: 'https://pc.test',
		token: 'shc_test',
		...(capabilities ? { native: { platform: 'android', bridge: 1, capabilities } } : {})
	};
	(window as TestWindow).shuffleShell = bridge;
}

afterEach(() => {
	call.active = false;
	call.hangup.mockReset();
	vi.restoreAllMocks();
	delete window.__SHUFFLE_MOBILE__;
	delete (window as TestWindow).shuffleShell;
});

describe('native shell messages', () => {
	it('posts the bar announcement and the Workers request as JSON strings', () => {
		const postMessage = vi.fn();
		inApp({ postMessage });
		expect(hasShellBridge()).toBe(true);
		expect(announceWorkerBar(true)).toBe(true);
		expect(announceWorkerBar(false)).toBe(true);
		expect(openNativeWorkers()).toBe(true);
		expect(postMessage.mock.calls.map(([raw]) => JSON.parse(raw))).toEqual([
			{ type: 'worker-bar', shown: true },
			{ type: 'worker-bar', shown: false },
			{ type: 'workers' }
		]);
	});

	it('asks for the app’s own Settings when the app offers app-settings', () => {
		const postMessage = vi.fn();
		inApp({ postMessage }, ['app-settings']);
		expect(nativeAppSettingsRoute()).toBe('settings');
		expect(openNativeAppSettings()).toBe(true);
		expect(postMessage.mock.calls.map(([raw]) => JSON.parse(raw))).toEqual([{ type: 'app-settings' }]);
	});

	it('keeps the Workers-to-Settings fallback until a minimum app build is enforced', () => {
		const postMessage = vi.fn();
		inApp({ postMessage }, ['worker-switch']);
		expect(nativeAppSettingsRoute()).toBe('workers');
		expect(openNativeAppSettings()).toBe(true);
		expect(postMessage.mock.calls.map(([raw]) => JSON.parse(raw))).toEqual([{ type: 'workers' }]);
	});

	it('does nothing in an app without the bridge', () => {
		inApp(undefined, ['app-settings']);
		expect(hasShellBridge()).toBe(false);
		expect(announceWorkerBar(true)).toBe(false);
		expect(openNativeWorkers()).toBe(false);
		expect(nativeAppSettingsRoute()).toBeNull();
		expect(openNativeAppSettings()).toBe(false);
	});

	it('does nothing in a plain browser, even if something named shuffleShell exists', () => {
		const postMessage = vi.fn();
		(window as TestWindow).shuffleShell = { postMessage };
		expect(hasShellBridge()).toBe(false);
		expect(announceWorkerBar(true)).toBe(false);
		expect(openNativeAppSettings()).toBe(false);
		expect(postMessage).not.toHaveBeenCalled();
	});

	it('reports a bridge that throws as not delivered', () => {
		inApp({ postMessage: () => { throw new Error('gone'); } });
		expect(announceWorkerBar(true)).toBe(false);
	});

	it('asks before opening Workers during a voice call, because the app closes chat and the call with it', () => {
		const postMessage = vi.fn();
		inApp({ postMessage }, ['worker-switch']);
		call.active = true;
		const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);

		expect(openNativeWorkers()).toBe(true);
		expect(postMessage).not.toHaveBeenCalled();
		expect(call.hangup).not.toHaveBeenCalled();

		expect(openNativeAppSettings()).toBe(true);
		expect(confirm).toHaveBeenCalledTimes(2);
		expect(call.hangup).toHaveBeenCalledWith('user');
		expect(postMessage.mock.calls.map(([raw]) => JSON.parse(raw))).toEqual([{ type: 'workers' }]);
	});
});
