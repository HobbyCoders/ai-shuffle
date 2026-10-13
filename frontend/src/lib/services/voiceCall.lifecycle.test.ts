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

import { startCall, hangup, toggleMic, toggleAgent, voiceCallState, type VoiceCallStatus } from './voiceCall';
import { setVoiceVolume } from './voiceVolume';

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
	sender = { track: null as unknown as MediaStreamTrack, replaceTrack: vi.fn(async (track: MediaStreamTrack) => { this.sender.track = track; }) };
	getSenders() { return [this.sender]; }
	addTrack(track: MediaStreamTrack) { this.sender.track = track; }
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

type MicTrack = { kind: string; enabled: boolean; readyState: string; stop: ReturnType<typeof vi.fn> };
let track: MicTrack;
const newMicTrack = (): MicTrack => {
 const t = { kind: 'audio', enabled: true, readyState: 'live', stop: vi.fn(() => { t.readyState = 'ended'; }) };
 return t;
};
const streamFor = (t: MicTrack) => ({ getTracks: () => [t], getAudioTracks: () => [t] });
let callSeq = 0;
let statuses: VoiceCallStatus[] = [];
let unsubscribe: () => void = () => {};

const flush = async () => {
	for (let i = 0; i < 25; i++) await Promise.resolve();
};

beforeEach(async () => {
	await hangup();
	vi.clearAllMocks();
	vi.useFakeTimers();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});
	peers.length = 0;
	callSeq = 0;
	track = newMicTrack();
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
 const devicesWith = (first: MicTrack) => {
  const devices = Object.assign(new EventTarget(), { getUserMedia: vi.fn().mockResolvedValue(streamFor(first)) });
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: devices });
  return devices;
 };
 it('mutes the pending sender immediately, not only after replaceTrack completes', async () => {
  const devices = devicesWith(track); await start();
  const next = newMicTrack(); devices.getUserMedia.mockResolvedValue(streamFor(next));
  let finish!: () => void;
  peers[0].sender.replaceTrack.mockImplementationOnce(async t => {
   peers[0].sender.track = t;
   await new Promise<void>(resolve => { finish = resolve; });
  });
  devices.dispatchEvent(new Event('devicechange')); await flush();
  toggleMic();
  expect(get(voiceCallState).micMuted).toBe(true);
  expect(peers[0].sender.track.enabled).toBe(false);
  finish(); await flush();
  expect(peers[0].sender.track.enabled).toBe(false);
 });
 it('attaches a fresh track disabled when already muted', async () => {
  const devices = devicesWith(track); await start(); toggleMic();
  const next = newMicTrack(); devices.getUserMedia.mockResolvedValue(streamFor(next));
  peers[0].sender.replaceTrack.mockImplementationOnce(async t => {
   expect(t.enabled).toBe(false); peers[0].sender.track = t;
  });
  devices.dispatchEvent(new Event('devicechange')); await flush();
  expect(peers[0].sender.track.enabled).toBe(false);
 });
 it('coalesces rapid device changes without overlapping acquisitions', async () => {
  const devices = devicesWith(track); await start();
  const first = newMicTrack(), last = newMicTrack();
  let finish!: (stream: ReturnType<typeof streamFor>) => void;
  devices.getUserMedia.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }))
   .mockResolvedValue(streamFor(last));
  for (let i = 0; i < 20; i++) devices.dispatchEvent(new Event('devicechange'));
  await flush(); expect(devices.getUserMedia).toHaveBeenCalledTimes(2);
  finish(streamFor(first)); await flush();
  expect(devices.getUserMedia).toHaveBeenCalledTimes(3);
  expect(peers[0].sender.track).toBe(last); expect(last.readyState).toBe('live');
  expect(first.stop).toHaveBeenCalledOnce(); expect(peers).toHaveLength(1);
 });
 it('finishes a pending refresh before the 50-minute peer handover', async () => {
  const devices = devicesWith(track); await start();
  const next = newMicTrack(); let finish!: (stream: ReturnType<typeof streamFor>) => void;
  devices.getUserMedia.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  devices.dispatchEvent(new Event('devicechange')); await flush();
  vi.advanceTimersByTime(50 * 60 * 1000); await flush();
  expect(api.mintVoiceSecret).not.toHaveBeenCalled(); expect(peers).toHaveLength(1);
  finish(streamFor(next)); await flush();
  expect(peers).toHaveLength(2); expect(peers[1].sender.track).toBe(next);
  expect(next.readyState).toBe('live'); expect(next.stop).not.toHaveBeenCalled();
  expect(get(voiceCallState).status).toBe('live');
 });
 it('queues a device refresh until the reconnect peer owns the microphone', async () => {
  const devices = devicesWith(track); await start();
  let finish!: (answer: string) => void;
  vi.mocked(fetch).mockImplementationOnce(async () => ({ ok: true,
   text: () => new Promise<string>(resolve => { finish = resolve; }),
   headers: new Headers({ Location: '/v1/realtime/calls/refreshed' }) }) as Response);
  vi.advanceTimersByTime(50 * 60 * 1000); await flush();
  expect(peers).toHaveLength(2);
  const next = newMicTrack(); devices.getUserMedia.mockResolvedValue(streamFor(next));
  devices.dispatchEvent(new Event('devicechange')); await flush();
  expect(devices.getUserMedia).toHaveBeenCalledOnce();
  finish('answer'); await flush();
  expect(peers[1].sender.track).toBe(next); expect(next.readyState).toBe('live');
  expect(track.stop).toHaveBeenCalledOnce(); expect(peers[0].connectionState).toBe('closed');
  expect(get(voiceCallState).status).toBe('live');
 });
 it('routes remote WebRTC audio through live gain and restores playback after unlock', async () => {
  await start();
  const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
  const limiter = { threshold: {}, knee: {}, ratio: {}, attack: {}, release: {}, connect: vi.fn(), disconnect: vi.fn() };
  const media = { connect: vi.fn(), disconnect: vi.fn() };
  const context = { state: 'running', createGain: () => gain, createDynamicsCompressor: () => limiter,
   createMediaStreamSource: vi.fn(() => media), destination: {}, resume: vi.fn(async () => {}), close: vi.fn(async () => {}) };
  vi.stubGlobal('AudioContext', class { constructor() { return context; } });
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
  setVoiceVolume(200);
  const remote = {};
  peers[0].dispatchEvent(Object.assign(new Event('track'), { streams: [remote] }));
  expect(media.connect).toHaveBeenCalledWith(gain);
  expect(context.createMediaStreamSource).toHaveBeenCalledWith(remote);
  expect(document.querySelector('audio')?.muted).toBe(true);
  expect(gain.gain.value).toBe(2);
  setVoiceVolume(0); expect(gain.gain.value).toBe(0);
  setVoiceVolume(250); expect(gain.gain.value).toBe(2.5);
  toggleAgent(); setVoiceVolume(300); expect(gain.gain.value).toBe(0);
  toggleAgent(); expect(gain.gain.value).toBe(3);
  context.resume.mockClear(); context.state = 'suspended';
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
  expect(context.resume).toHaveBeenCalledOnce();
  await hangup(); expect(context.close).toHaveBeenCalledOnce();
  setVoiceVolume(100);
 });
 it('reacquires the default mic on device change without replacing the peer or call', async () => {
  const devices = Object.assign(new EventTarget(), { getUserMedia: vi.fn() });
  const old = track;
  devices.getUserMedia.mockResolvedValueOnce({ getTracks: () => [old], getAudioTracks: () => [old] });
  const next = newMicTrack();
  devices.getUserMedia.mockResolvedValue({ getTracks: () => [next], getAudioTracks: () => [next] });
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: devices });
  await start();
  devices.dispatchEvent(new Event('devicechange')); await flush();
  expect(devices.getUserMedia).toHaveBeenCalledTimes(2);
  expect(devices.getUserMedia.mock.calls[1][0].audio).not.toHaveProperty('deviceId');
  expect(peers[0].sender.replaceTrack).toHaveBeenCalledWith(next);
  expect(old.stop).toHaveBeenCalledOnce();
  expect(get(voiceCallState).status).toBe('live'); expect(peers).toHaveLength(1);
  await hangup();
  devices.dispatchEvent(new Event('devicechange')); await flush();
  expect(devices.getUserMedia).toHaveBeenCalledTimes(2);
 });
	it('connects (connecting → live), registers the call id, and hangup leaves nothing running', async () => {
		await start();

		expect(statuses.slice(-2)).toEqual(['connecting', 'live']);
		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', status: 'live', error: null });
		expect(get(voiceCallState).startedAt).not.toBeNull();
		expect(api.attachVoiceCall).toHaveBeenCalledWith('owner', 'call-1');
		expect(vi.getTimerCount()).toBe(1); // only the 50-minute re-handshake

		await hangup();

		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', status: 'ended' });
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'user');
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
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'webrtc-failed');
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
