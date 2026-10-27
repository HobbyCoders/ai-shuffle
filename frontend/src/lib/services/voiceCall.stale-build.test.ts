/**
 * A call started on a page that outlived a worker update (#707).
 *
 * The page's lazily loaded voice audio stream module no longer exists on the
 * worker: the import 404s. Before, the call's error was the browser's raw
 * import message and, on a phone, nothing on screen at all. The start must
 * end in the call-outcome banner with a Reload. The wording stays neutral
 * (a dropped connection fails the same way); the update watcher's version
 * check is what says whether the app was updated.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';

const api = vi.hoisted(() => ({
	mintVoiceSecret: vi.fn(),
	attachVoiceCall: vi.fn(),
	hangupVoiceCall: vi.fn(async () => ({ ok: true })),
	getVoiceCallState: vi.fn(),
	prepareConversationCall: vi.fn()
}));
vi.mock('$lib/api/voice', () => api);
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn(() => Promise.resolve({})) } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));

// What Chromium (Android WebView included) rejects the import with when the
// chunk is gone. Thrown as the module's export is read, which is where the
// call's own `await import()` hands it over.
const missing = vi.hoisted(() => ({ fail: true }));
vi.mock('./voiceAudioStream', () => ({
	get openVoiceAudioStream() {
		if (missing.fail) {
			throw new TypeError('Failed to fetch dynamically imported module: https://worker.example/mobile/_app/immutable/chunks/Old.js');
		}
		return vi.fn();
	}
}));

import { hangup, startCall, voiceCallState } from './voiceCall';
import { watchCallOutcomes } from './voiceCallOutcome';
import { CALL_LOAD_FAILED_MESSAGE, canReloadSilently, setAppUpdateListener } from './app-update';
import { notifications, notify } from '$lib/stores/notifications';

const track = { kind: 'audio', enabled: true, readyState: 'live', stop: vi.fn() };
const chunkFailed = vi.fn();

beforeEach(async () => {
	await hangup();
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	vi.clearAllMocks();
	missing.fail = true;
	setAppUpdateListener({ reconnected: vi.fn(), chunkFailed });
	Object.defineProperty(navigator, 'mediaDevices', {
		configurable: true,
		value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }) }
	});
	vi.stubGlobal('AudioContext', class {});
});

afterEach(async () => {
	setAppUpdateListener(null);
	await hangup();
	vi.unstubAllGlobals();
});

it('ends a start whose voice module is gone with "reload to continue", not the raw import error', async () => {
	await startCall('fresh', { client_secret: 'unused', model: 'gpt-live-1-codex' });

	expect(get(voiceCallState)).toMatchObject({ status: 'error', sessionId: 'fresh', error: CALL_LOAD_FAILED_MESSAGE });
	// The app-update watcher hears about it, so it can confirm the skew.
	expect(chunkFailed).toHaveBeenCalledTimes(1);
	// The microphone it had acquired is released.
	expect(track.stop).toHaveBeenCalled();
});

it('puts it in the outcome banner with a Reload action', async () => {
	const source = writable(get(voiceCallState));
	const unsubscribe = voiceCallState.subscribe((value) => source.set(value));
	watchCallOutcomes(source);

	await startCall('fresh', { client_secret: 'unused', model: 'gpt-live-1-codex' });

	const banner = notifications.current.live.find((n) => n.message === CALL_LOAD_FAILED_MESSAGE);
	expect(banner).toMatchObject({ surface: 'banner', severity: 'warning', session_id: 'fresh', action: { label: 'Reload' } });
	unsubscribe();
});

it('a live call blocks an automatic reload; an ended one does not', async () => {
	missing.fail = false;
	expect(canReloadSilently()).toBe(true);
	voiceCallState.update((s) => ({ ...s, sessionId: 's', status: 'live' }));
	expect(canReloadSilently()).toBe(false);
	await hangup();
	expect(canReloadSilently()).toBe(true);
});
