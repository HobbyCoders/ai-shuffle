import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openNativeVoice } from './nativeVoice';

function installBridge() {
	const events = new EventTarget();
	const postMessage = vi.fn();
	const bridge = { postMessage, addEventListener: events.addEventListener.bind(events), removeEventListener: vi.fn(events.removeEventListener.bind(events)) };
	Object.defineProperty(window, 'shuffleNativeVoice', { configurable: true, value: bridge });
	return { bridge, postMessage, reply: (value: unknown) => events.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })) };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => { delete (window as Window & { shuffleNativeVoice?: unknown }).shuffleNativeVoice; vi.useRealTimers(); });

describe('Android voice handoff', () => {
	it('leaves normal browser transport available when native transport is absent', async () => {
		expect(await openNativeVoice('session-1')).toBe(false);
	});
	it('sends only the resolved session and accepts its correlated acknowledgement', async () => {
		const fixture = installBridge();
		const result = openNativeVoice('session-1');
		const request = JSON.parse(fixture.postMessage.mock.calls[0][0]);
		expect(request).toEqual({ type: 'start', id: expect.any(String), sessionId: 'session-1' });
		fixture.reply({ ...request, ok: true });
		expect(await result).toBe(true);
		expect(fixture.bridge.removeEventListener).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
	});
	it('ignores acknowledgements for another request or conversation', async () => {
		const fixture = installBridge();
		const result = openNativeVoice('session-1');
		const request = JSON.parse(fixture.postMessage.mock.calls[0][0]);
		fixture.reply({ ...request, id: 'wrong', ok: true });
		fixture.reply({ ...request, sessionId: 'other', ok: true });
		expect(vi.getTimerCount()).toBe(1);
		fixture.reply({ ...request, ok: true });
		expect(await result).toBe(true);
	});
	it('deduplicates repeated starts and does not switch a pending conversation', async () => {
		const fixture = installBridge();
		const result = openNativeVoice('session-1');
		expect(openNativeVoice('session-1')).toBe(result);
		await expect(openNativeVoice('session-2')).rejects.toThrow('Another voice session');
		expect(fixture.postMessage).toHaveBeenCalledOnce();
		fixture.reply({ ...JSON.parse(fixture.postMessage.mock.calls[0][0]), ok: true });
		await result;
	});
	it('surfaces native refusal without claiming success or falling back to web capture', async () => {
		const fixture = installBridge();
		const result = openNativeVoice('session-1');
		fixture.reply({ ...JSON.parse(fixture.postMessage.mock.calls[0][0]), ok: false, error: 'End the current call first.' });
		await expect(result).rejects.toThrow('End the current call first.');
	});
	it('expires a missing acknowledgement and releases the request for retry', async () => {
		const fixture = installBridge();
		const result = openNativeVoice('session-1');
		const rejected = expect(result).rejects.toThrow('Android voice did not open');
		await vi.advanceTimersByTimeAsync(5_000);
		await rejected;
		const retry = openNativeVoice('session-1');
		fixture.reply({ ...JSON.parse(fixture.postMessage.mock.calls[1][0]), ok: true });
		expect(await retry).toBe(true);
	});
	it('treats a native refusal with an empty error as a failure', async () => {
		const fixture = installBridge();
		for (const error of ['', '   ']) {
			const result = openNativeVoice('session-1');
			fixture.reply({ ...JSON.parse(fixture.postMessage.mock.lastCall![0]), ok: false, error });
			await expect(result).rejects.toThrow('Android voice did not open');
		}
	});
	it('rejects invalid session identifiers without sending native commands', async () => {
		const fixture = installBridge();
		await expect(openNativeVoice('../other?secret=x')).rejects.toThrow('Select a voice session');
		expect(fixture.postMessage).not.toHaveBeenCalled();
	});
});
