/**
 * Streamed calls (GPT-Live subscription, Grok, PersonaPlex) feed the dock's
 * meter from the audio stream's own capture graph.
 *
 * Phone report: a SuperGrok call transcribed the caller while the waveform
 * stayed flat. Streamed engines send no client-side VAD events, so
 * `userSpeaking` never became true, and the level came from a SECOND
 * AudioContext created long after the tap and never resumed.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({
	mintVoiceSecret: vi.fn(),
	attachVoiceCall: vi.fn(),
	hangupVoiceCall: vi.fn(async () => ({ ok: true })),
	getVoiceCallState: vi.fn(),
	prepareConversationCall: vi.fn()
}));
vi.mock('$lib/api/voice', () => api);

type Handlers = Parameters<typeof import('./voiceAudioStream').openVoiceAudioStream>[2];
const audio = vi.hoisted(() => ({ handlers: null as Handlers | null, open: vi.fn() }));
vi.mock('./voiceAudioStream', () => ({ openVoiceAudioStream: audio.open }));

import { hangup, startCall, toggleMic, voiceCallState } from './voiceCall';

const contexts = vi.fn();
const track = { kind: 'audio', enabled: true, readyState: 'live', stop: vi.fn() };
const muteInput = vi.fn();

beforeEach(async () => {
	await hangup();
	vi.clearAllMocks();
	audio.handlers = null;
	Object.defineProperty(navigator, 'mediaDevices', {
		configurable: true,
		value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }) }
	});
	// Any AudioContext beside the stream's own (mocked away here) is a meter
	// the browser may never start.
	vi.stubGlobal('AudioContext', class { constructor() { contexts(); } });
	audio.open.mockImplementation(async (_session: string, _mic: MediaStream, handlers: Handlers) => {
		audio.handlers = handlers;
		return { close: vi.fn(async () => {}), muteOutput: vi.fn(), muteInput, sendText: vi.fn() };
	});
});

afterEach(async () => {
	await hangup();
	vi.unstubAllGlobals();
});

it('moves the meter and "hearing you" from the stream, without a second AudioContext', async () => {
	await startCall('owner', { client_secret: 'unused', model: 'grok-voice-supergrok' });
	expect(get(voiceCallState).status).toBe('live');
	expect(contexts).not.toHaveBeenCalled();
	expect(audio.handlers?.level).toBeTypeOf('function');

	audio.handlers!.level!(0.8, true);
	expect(get(voiceCallState).userSpeaking).toBe(true);
	expect(get(voiceCallState).level).toBeGreaterThan(0.4);

	audio.handlers!.level!(0.2, false);
	expect(get(voiceCallState).userSpeaking).toBe(false);
	audio.handlers!.level!(0, false);
	expect(get(voiceCallState).level).toBe(0);
});

it('ignores meter frames from a call that has ended', async () => {
	await startCall('owner', { client_secret: 'unused', model: 'gpt-live-1-codex' });
	const stale = audio.handlers!;
	await hangup();
	stale.level!(0.9, true);
	expect(get(voiceCallState)).toMatchObject({ status: 'ended', level: 0, userSpeaking: false });
});

it('mute goes through the stream, which zeroes the meter', async () => {
	await startCall('owner', { client_secret: 'unused', model: 'gpt-live-1-codex' });
	toggleMic();
	expect(muteInput).toHaveBeenLastCalledWith(true);
});
