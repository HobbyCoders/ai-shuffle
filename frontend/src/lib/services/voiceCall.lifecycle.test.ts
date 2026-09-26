/**
 * Voice call controller — state transitions on connect / drop / 50-minute
 * re-handshake / hangup, and that no timer or mic track outlives the call.
 *
 * WebRTC is faked with a minimal RTCPeerConnection whose connection state the
 * test drives; the voice API module is mocked. Timers are fake so the
 * 50-minute re-handshake can be reached and pending timers counted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({
	mintVoiceSecret: vi.fn(),
	attachVoiceCall: vi.fn(),
	hangupVoiceCall: vi.fn(),
	getVoiceCallState: vi.fn()
}));
vi.mock('$lib/api/voice', () => api);

import { startCall, hangup, voiceCallState, type VoiceCallStatus } from './voiceCall';

const peers: Peer[] = [];
class Peer extends EventTarget {
	connectionState = 'connected';
	channel = Object.assign(new EventTarget(), { send: vi.fn(), readyState: 'open', close: vi.fn() });
	constructor() {
		super();
		peers.push(this);
	}
	createDataChannel() {
		return this.channel;
	}
	addTrack() {}
	async createOffer() {
		return { sdp: 'offer' };
	}
	async setLocalDescription() {}
	async setRemoteDescription() {}
	close() {
		this.connectionState = 'closed';
	}
	fail() {
		this.connectionState = 'failed';
		this.dispatchEvent(new Event('connectionstatechange'));
	}
}

let track: { enabled: boolean; stop: ReturnType<typeof vi.fn> };
let callSeq = 0;
let statuses: VoiceCallStatus[] = [];
let unsubscribe: () => void = () => {};

const flush = async () => {
	for (let i = 0; i < 5; i++) await Promise.resolve();
};

beforeEach(async () => {
	await hangup();
	vi.clearAllMocks();
	vi.useFakeTimers();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});
	peers.length = 0;
	callSeq = 0;
	track = { enabled: true, stop: vi.fn() };
	const mic = { getTracks: () => [track], getAudioTracks: () => [track] };
	Object.defineProperty(navigator, 'mediaDevices', {
		configurable: true,
		value: { getUserMedia: vi.fn().mockResolvedValue(mic) }
	});
	vi.stubGlobal('RTCPeerConnection', Peer);
	vi.stubGlobal(
		'fetch',
		vi.fn(async () => ({
			ok: true,
			text: async () => 'answer',
			headers: new Headers({ Location: `/v1/realtime/calls/call-${++callSeq}` })
		}))
	);
	api.attachVoiceCall.mockResolvedValue({ ok: true });
	api.hangupVoiceCall.mockResolvedValue({ ok: true });
	api.mintVoiceSecret.mockResolvedValue({ client_secret: 'fresh', model: 'test-model' });
	statuses = [];
	unsubscribe = voiceCallState.subscribe((s) => {
		if (statuses.at(-1) !== s.status) statuses.push(s.status);
	});
});

afterEach(async () => {
	unsubscribe();
	await hangup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

const start = () => startCall('owner', { client_secret: 'secret', model: 'test-model' });

describe('voice call lifecycle', () => {
	it('connects (connecting → live), registers the call id, and hangup leaves nothing running', async () => {
		await start();

		expect(statuses.slice(-2)).toEqual(['connecting', 'live']);
		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', status: 'live', error: null });
		expect(get(voiceCallState).startedAt).not.toBeNull();
		expect(api.attachVoiceCall).toHaveBeenCalledWith('owner', 'call-1');
		expect(vi.getTimerCount()).toBe(1); // only the 50-minute re-handshake

		await hangup();

		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', status: 'ended' });
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner');
		expect(peers[0].connectionState).toBe('closed');
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(60 * 60_000);
		expect(api.mintVoiceSecret).not.toHaveBeenCalled();
	});

	it('a mid-call connection failure ends the call with the reason on screen', async () => {
		await start();

		peers[0].fail();
		await flush();

		expect(get(voiceCallState)).toMatchObject({
			sessionId: 'owner',
			status: 'error',
			error: 'The voice connection dropped.'
		});
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner');
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(vi.getTimerCount()).toBe(0);
	});

	it('re-handshakes at 50 minutes onto a new connection, and still notices when that one drops', async () => {
		await start();

		await vi.advanceTimersByTimeAsync(50 * 60_000);

		expect(api.mintVoiceSecret).toHaveBeenCalledWith('owner');
		expect(peers).toHaveLength(2);
		expect(api.attachVoiceCall).toHaveBeenLastCalledWith('owner', 'call-2');
		expect(peers[0].connectionState).toBe('closed');
		expect(get(voiceCallState).status).toBe('live');
		expect(statuses.slice(-2)).toEqual(['reconnecting', 'live']);
		expect(track.stop).not.toHaveBeenCalled(); // the mic survives the swap
		expect(vi.getTimerCount()).toBe(1); // re-armed for the next wall

		peers[1].fail();
		await flush();

		expect(get(voiceCallState)).toMatchObject({ status: 'error', error: 'The voice connection dropped.' });
		expect(vi.getTimerCount()).toBe(0);
	});

	it('a refused offer ends in error with the mic released and no timers', async () => {
		vi.mocked(fetch).mockResolvedValueOnce({
			ok: false,
			status: 500,
			text: async () => 'upstream exploded',
			headers: new Headers()
		} as Response);

		await start();

		const state = get(voiceCallState);
		expect(state.status).toBe('error');
		expect(state.error).toBe('OpenAI rejected the call (HTTP 500): upstream exploded');
		expect(api.attachVoiceCall).not.toHaveBeenCalled();
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(vi.getTimerCount()).toBe(0);
	});
});
