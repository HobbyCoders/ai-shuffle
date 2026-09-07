import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
const api = vi.hoisted(() => ({ mintVoiceSecret: vi.fn(), attachVoiceCall: vi.fn(), hangupVoiceCall: vi.fn(), getVoiceCallState: vi.fn() }));
vi.mock('$lib/api/voice', () => api);
import { startCall, startCallForSession, hangup, voiceCallState } from './voiceCall';

let events: EventTarget;
let post: ReturnType<typeof vi.fn>;
let capture: ReturnType<typeof vi.fn>;
beforeEach(async () => {
	await hangup();
	vi.clearAllMocks();
	events = new EventTarget();
	post = vi.fn((raw: string) => {
		const request = JSON.parse(raw);
		queueMicrotask(() => events.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ ...request, ok: true }) })));
	});
	Object.defineProperty(window, 'shuffleNativeVoice', { configurable: true, value: {
		postMessage: post, addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events)
	} });
	capture = vi.fn();
	Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: capture } });
	vi.stubGlobal('fetch', vi.fn());
});
afterEach(async () => {
	delete (window as Window & { shuffleNativeVoice?: unknown }).shuffleNativeVoice;
	await hangup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('voice controller native transport selection', () => {
	it('hands off the freshly created session without exposing its secret or acquiring browser audio', async () => {
		await startCall('created-with-selected-context', { client_secret: 'must-stay-out-of-bridge', model: 'voice-model' });
		expect(JSON.parse(post.mock.calls[0][0])).toMatchObject({ sessionId: 'created-with-selected-context' });
		expect(post.mock.calls[0][0]).not.toContain('must-stay-out-of-bridge');
		expect(capture).not.toHaveBeenCalled();
		expect(fetch).not.toHaveBeenCalled();
		expect(api.attachVoiceCall).not.toHaveBeenCalled();
		expect(get(voiceCallState)).toMatchObject({ sessionId: 'created-with-selected-context', status: 'idle', notice: 'Voice opened in the Android app.' });
	});
	it('hands off an existing session before reminting or modifying backend context in the browser', async () => {
		await startCallForSession('existing-session');
		expect(JSON.parse(post.mock.calls[0][0])).toMatchObject({ sessionId: 'existing-session' });
		expect(api.mintVoiceSecret).not.toHaveBeenCalled();
		expect(capture).not.toHaveBeenCalled();
		expect(fetch).not.toHaveBeenCalled();
	});
	it('reports native refusal without attempting the CSP-blocked browser transport', async () => {
		post.mockImplementation((raw: string) => {
			const request = JSON.parse(raw);
			queueMicrotask(() => events.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ ...request, ok: false, error: 'Open this worker again.' }) })));
		});
		await startCallForSession('existing-session');
		expect(get(voiceCallState)).toMatchObject({ status: 'error', error: 'Open this worker again.' });
		expect(api.mintVoiceSecret).not.toHaveBeenCalled();
		expect(capture).not.toHaveBeenCalled();
		expect(fetch).not.toHaveBeenCalled();
	});
});
