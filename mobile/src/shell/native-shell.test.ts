import { afterEach, describe, expect, it, vi } from 'vitest';
import { announceWorkerBar, hasShellBridge, openNativeWorkers } from './native-shell';

type TestWindow = Window & { shuffleShell?: unknown };

function inApp(bridge?: unknown) {
	window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://pc.test', token: 'shc_test' };
	(window as TestWindow).shuffleShell = bridge;
}

afterEach(() => {
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

	it('does nothing in an app without the bridge', () => {
		inApp(undefined);
		expect(hasShellBridge()).toBe(false);
		expect(announceWorkerBar(true)).toBe(false);
		expect(openNativeWorkers()).toBe(false);
	});

	it('does nothing in a plain browser, even if something named shuffleShell exists', () => {
		const postMessage = vi.fn();
		(window as TestWindow).shuffleShell = { postMessage };
		expect(hasShellBridge()).toBe(false);
		expect(announceWorkerBar(true)).toBe(false);
		expect(postMessage).not.toHaveBeenCalled();
	});

	it('reports a bridge that throws as not delivered', () => {
		inApp({ postMessage: () => { throw new Error('gone'); } });
		expect(announceWorkerBar(true)).toBe(false);
	});
});
