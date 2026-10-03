import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
const api = vi.hoisted(() => ({ prepareConversationCall: vi.fn(), mintVoiceSecret: vi.fn(), attachVoiceCall: vi.fn(), hangupVoiceCall: vi.fn(), getVoiceCallState: vi.fn() }));
vi.mock('$lib/api/voice', () => api);
const streamed = vi.hoisted(() => ({ close: vi.fn(), muteInput: vi.fn(), muteOutput: vi.fn(), sendText: vi.fn() }));
const streamHandlers = vi.hoisted(() => ({ current: null as null | { closed(message: string | null, reason: string): void; reconnecting?(active: boolean): void } }));
vi.mock('./voiceAudioStream', () => ({
	openVoiceAudioStream: vi.fn(async (_session: string, _mic: unknown, handlers: typeof streamHandlers.current) => {
		streamHandlers.current = handlers;
		return streamed;
	}),
}));
import { startCall, startCallForSession, hangup, voiceCallState, sendVoiceText, reconcileVoiceCall, confirmCallReplacement, toggleMic, STREAM_DROPPED_MESSAGE, STREAM_RECONNECT_BACKOFF_MS, STREAM_RECONNECT_WINDOW_MS, STREAM_ATTEMPT_TIMEOUT_MS } from './voiceCall';

let channel: EventTarget & { send: ReturnType<typeof vi.fn>; readyState: string; close: ReturnType<typeof vi.fn> };
let track: { enabled: boolean; stop: ReturnType<typeof vi.fn> };
let capture: ReturnType<typeof vi.fn>;
class Peer extends EventTarget {
	connectionState = 'connected';
	createDataChannel() { return channel; }
	addTrack() {}
	async createOffer() { return { sdp: 'offer' }; }
	async setLocalDescription() {}
	async setRemoteDescription() {}
	close() { this.connectionState = 'closed'; }
}
beforeEach(async () => {
	await hangup();
	vi.clearAllMocks();
	streamed.sendText.mockResolvedValue(true);
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	channel = Object.assign(new EventTarget(), { send: vi.fn(), readyState: 'open', close: vi.fn() });
	track = { enabled: true, stop: vi.fn() };
	capture = vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] });
	Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: capture } });
	vi.stubGlobal('RTCPeerConnection', Peer);
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, text: async () => 'answer', headers: new Headers({ Location: '/calls/test-call' }) }));
	api.mintVoiceSecret.mockResolvedValue({ client_secret: 'test', model: 'test-model' });
	api.getVoiceCallState.mockResolvedValue({ active: true });
});
afterEach(async () => { await hangup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const start = () => startCall('owner', { client_secret: 'test', model: 'test-model' });

describe('voice owner lifecycle', () => {
 it('uses the live streaming owner for attachments and gates microphone mute on that transport', async () => {
  await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
  expect(sendVoiceText('other', 'Wrong owner')).toBe(false);
  toggleMic();
  expect(track.enabled).toBe(false);
  expect(streamed.muteInput).toHaveBeenLastCalledWith(true);
  expect(await sendVoiceText('owner', 'Inspect /workspace/uploads/report.pdf')).toBe(true);
  expect(streamed.sendText).toHaveBeenCalledWith('Inspect /workspace/uploads/report.pdf');
  expect(channel.send).not.toHaveBeenCalled();
  toggleMic();
  expect(track.enabled).toBe(true);
  expect(streamed.muteInput).toHaveBeenLastCalledWith(false);
 });
 it('reports unsupported PersonaPlex text accurately without losing the draft', async () => {
  await startCall('owner', { client_secret: 'test', model: 'fal-ai/personaplex/realtime' });
  expect(() => sendVoiceText('owner', 'Keep me')).toThrow('does not support text or attachments');
  expect(streamed.sendText).not.toHaveBeenCalled();
 });
 it('attaches ordinary audio to the same conversation using the returned credential, without a second mint', async () => {
  api.prepareConversationCall.mockResolvedValue({ session_id: 'owner', client_secret: 'attached', model: 'test-model' });
  await startCallForSession('owner', { attach: true });
  expect(api.prepareConversationCall).toHaveBeenCalledWith('owner');
  expect(api.mintVoiceSecret).not.toHaveBeenCalled();
  expect(get(voiceCallState).sessionId).toBe('owner');
  expect(capture).toHaveBeenCalledTimes(1);
 });
 it('does not capture microphone audio after attachment is refused', async () => {
  api.prepareConversationCall.mockRejectedValue({ detail: 'Conversation is not eligible', status: 404 });
  await startCallForSession('owner', { attach: true });
  expect(get(voiceCallState)).toMatchObject({ status: 'error', error: 'Conversation is not eligible' });
  expect(api.mintVoiceSecret).not.toHaveBeenCalled();
  expect(capture).not.toHaveBeenCalled();
 });

	it('serializes rapid typed sends before response.created arrives', async () => {
		await start();
		expect(sendVoiceText('owner', 'first')).toBe(true);
		expect(sendVoiceText('owner', 'second')).toBe(true);
		expect(sendVoiceText('owner', 'third')).toBe(true);
		const types = () => channel.send.mock.calls.map(([raw]) => JSON.parse(raw).type);
		expect(types().filter(t => t === 'response.create')).toHaveLength(1);
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'response.done' }) }));
		expect(types().filter(t => t === 'response.create')).toHaveLength(2);
	});
	it('retains the draft when the item cannot enter the transport', async () => {
		await start();
		channel.send.mockImplementationOnce(() => { throw new Error('closed'); });
		expect(sendVoiceText('owner', 'keep me')).toBe(false);
		expect(channel.send).toHaveBeenCalledTimes(1);
	});
	it('does not restore and duplicate a sent item when reply scheduling fails', async () => {
		await start();
		channel.send.mockImplementationOnce(() => {}).mockImplementationOnce(() => { throw new Error('closed'); });
		expect(sendVoiceText('owner', 'already sent')).toBe(true);
		expect(get(voiceCallState).notice).toContain('reply could not start');
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'response.done' }) }));
		expect(channel.send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual([
			'conversation.item.create', 'response.create', 'response.create'
		]);
	});
	it('retries the response after a VAD scheduling collision without repeating text', async () => {
		await start();
		sendVoiceText('owner', 'typed while VAD starts');
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'error', error: { code: 'conversation_already_has_active_response' } }) }));
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'response.done' }) }));
		expect(channel.send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual([
			'conversation.item.create', 'response.create', 'response.create'
		]);
	});
	it('routes typed text only into the live owner conversation', async () => {
		await start();
		expect(sendVoiceText('other', 'wrong conversation')).toBe(false);
		expect(channel.send).not.toHaveBeenCalled();
		expect(sendVoiceText('owner', 'Use the selected branch')).toBe(true);
		expect(JSON.parse(channel.send.mock.calls[0][0]).item.content).toEqual([{ type: 'input_text', text: 'Use the selected branch' }]);
		expect(JSON.parse(channel.send.mock.calls[1][0]).type).toBe('response.create');
	});
	it('queues a typed response until the running response finishes', async () => {
		await start();
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'response.created' }) }));
		expect(sendVoiceText('owner', 'Also run the tests')).toBe(true);
		expect(channel.send).toHaveBeenCalledTimes(1);
		channel.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'response.done' }) }));
		expect(JSON.parse(channel.send.mock.calls[1][0]).type).toBe('response.create');
	});
	it('does not reacquire the microphone when the owner is reopened', async () => {
		await start();
		toggleMic();
		await startCallForSession('owner');
		expect(capture).toHaveBeenCalledTimes(1);
		expect(track.enabled).toBe(false);
		expect(api.mintVoiceSecret).not.toHaveBeenCalled();
	});
	it('keeps the current call when replacement is declined', async () => {
		await start();
		vi.spyOn(window, 'confirm').mockReturnValue(false);
		await startCallForSession('other');
		expect(get(voiceCallState).sessionId).toBe('owner');
		expect(track.stop).not.toHaveBeenCalled();
	});
	it('ends the owner before an explicitly confirmed replacement', async () => {
		await start();
		vi.spyOn(window, 'confirm').mockReturnValue(true);
		expect(await confirmCallReplacement('other')).toBe(true);
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'replaced');
		expect(track.stop).toHaveBeenCalledTimes(1);
	});
	it('reconciles a remotely ended call without minting or restarting it', async () => {
		await start();
		api.getVoiceCallState.mockResolvedValue({ active: false });
		await reconcileVoiceCall();
		expect(get(voiceCallState).status).toBe('ended');
		await reconcileVoiceCall();
		expect(api.getVoiceCallState).toHaveBeenCalledTimes(1);
		expect(api.mintVoiceSecret).not.toHaveBeenCalled();
		expect(capture).toHaveBeenCalledTimes(1);
	});
	// #1671: an unexpected end must never look like the End button.
	it('reports a provider-ended streamed call as an ending with a notice and its reason', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		streamHandlers.current!.closed('The voice provider ended the call.', 'provider-closed');
		await vi.waitFor(() => expect(get(voiceCallState).notice).toBe('The voice provider ended the call.'));
		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', status: 'ended', error: null, endReason: 'provider-closed' });
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'provider-closed');
		expect(console.info).toHaveBeenCalledWith('[voice] call ended:', 'provider-closed');
	});
	it('reopens a dropped audio connection on the same call', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		toggleMic();
		streamHandlers.current!.closed('The voice connection dropped.', 'connection-dropped');
		expect(get(voiceCallState).status).toBe('reconnecting');
		await vi.waitFor(() => expect(get(voiceCallState).status).toBe('live'));
		expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(2);
		expect(vi.mocked(openVoiceAudioStream).mock.calls[0][5]).toBe(false);
		expect(vi.mocked(openVoiceAudioStream).mock.calls[1][5]).toBe(true);
		expect(streamed.close).toHaveBeenCalledTimes(1); // The dropped stream's audio graph.
		expect(streamed.muteInput).toHaveBeenLastCalledWith(true); // Mute survives the reconnect.
		expect(api.hangupVoiceCall).not.toHaveBeenCalled();
		expect(get(voiceCallState)).toMatchObject({ sessionId: 'owner', error: null, endReason: null });
	});
	// When each reconnect attempt starts, in ms after the drop, if every attempt fails at once.
	const attemptTimes = () => {
		const times: number[] = [];
		for (let attempt = 1, at = 0; ; attempt++) {
			const delay = STREAM_RECONNECT_BACKOFF_MS[Math.min(attempt, STREAM_RECONNECT_BACKOFF_MS.length) - 1];
			if (attempt > 1 && at + delay > STREAM_RECONNECT_WINDOW_MS) return times;
			at += delay;
			times.push(at);
		}
	};
	it('keeps trying for about 30 seconds with backoff, then ends with a plain message', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		const times = attemptTimes();
		expect(times).toEqual([500, 1500, 3500, 7500, 11500, 15500, 19500, 23500, 27500]);
		for (const _ of times) vi.mocked(openVoiceAudioStream).mockRejectedValueOnce(new Error('no close frame received or sent'));
		vi.useFakeTimers();
		try {
			streamHandlers.current!.closed('The voice connection dropped.', 'connection-dropped');
			await vi.advanceTimersByTimeAsync(25_000);
			// Still trying, and the caller still sees it.
			expect(get(voiceCallState).status).toBe('reconnecting');
			expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(1 + times.filter(t => t <= 25_000).length);
			await vi.advanceTimersByTimeAsync(STREAM_RECONNECT_WINDOW_MS);
		} finally {
			vi.useRealTimers();
		}
		await vi.waitFor(() => expect(get(voiceCallState).error).toBe(STREAM_DROPPED_MESSAGE));
		expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(1 + times.length);
		expect(get(voiceCallState)).toMatchObject({ status: 'error', endReason: 'connection-dropped' });
		expect(STREAM_DROPPED_MESSAGE).not.toContain('close frame');
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'connection-dropped');
	});
	it('gives up on a hanging reconnect attempt after its limit and tries again', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		vi.mocked(openVoiceAudioStream).mockImplementationOnce((_s, _m, _h, signal) => new Promise((_, reject) => {
			signal.addEventListener('abort', () => reject(new Error('Voice connection cancelled.')), { once: true });
		}));
		vi.useFakeTimers();
		try {
			streamHandlers.current!.closed('The voice connection dropped.', 'connection-dropped');
			await vi.advanceTimersByTimeAsync(STREAM_RECONNECT_BACKOFF_MS[0] + STREAM_ATTEMPT_TIMEOUT_MS - 100);
			expect(vi.mocked(openVoiceAudioStream).mock.calls[1][3].aborted).toBe(false);
			await vi.advanceTimersByTimeAsync(100 + STREAM_RECONNECT_BACKOFF_MS[1] + 10);
		} finally {
			vi.useRealTimers();
		}
		await vi.waitFor(() => expect(get(voiceCallState).status).toBe('live'));
		expect(vi.mocked(openVoiceAudioStream).mock.calls[1][3].aborted).toBe(true);
		expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(3);
	});
	it('stops reconnecting when the caller hangs up meanwhile', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		streamHandlers.current!.closed('The voice connection dropped.', 'connection-dropped');
		await hangup();
		await new Promise(resolve => setTimeout(resolve, STREAM_RECONNECT_BACKOFF_MS[0] + 50));
		expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(1);
		expect(get(voiceCallState)).toMatchObject({ status: 'ended', endReason: 'user', error: null });
	});
	it('cancels an in-flight reconnect attempt at once on hang-up', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		vi.mocked(openVoiceAudioStream).mockImplementationOnce((_s, _m, _h, signal) => new Promise((_, reject) => {
			signal.addEventListener('abort', () => reject(new Error('Voice connection cancelled.')), { once: true });
		}));
		streamHandlers.current!.closed('The voice connection dropped.', 'connection-dropped');
		await vi.waitFor(() => expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(2));
		await hangup();
		expect(vi.mocked(openVoiceAudioStream).mock.calls[1][3].aborted).toBe(true);
		expect(get(voiceCallState)).toMatchObject({ status: 'ended', endReason: 'user', error: null });
	});
	it('shows reconnecting while the server restores its provider connection', async () => {
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		streamHandlers.current!.reconnecting!(true);
		expect(get(voiceCallState).status).toBe('reconnecting');
		streamHandlers.current!.reconnecting!(false);
		expect(get(voiceCallState).status).toBe('live');
		expect(api.hangupVoiceCall).not.toHaveBeenCalled();
	});
	it('reports a server error on a streamed call without reconnecting', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		const { openVoiceAudioStream } = await import('./voiceAudioStream');
		streamHandlers.current!.closed('The voice connection dropped and could not be restored.', 'provider-error');
		await vi.waitFor(() => expect(get(voiceCallState).error).toBe('The voice connection dropped and could not be restored.'));
		expect(get(voiceCallState)).toMatchObject({ status: 'error', endReason: 'provider-error' });
		expect(vi.mocked(openVoiceAudioStream)).toHaveBeenCalledTimes(1);
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'provider-error');
	});
	it('labels the End button hangup as the user ending the call', async () => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		await startCall('owner', { client_secret: 'test', model: 'gpt-live-1' });
		await hangup();
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner', 'user');
		expect(get(voiceCallState).endReason).toBe('user');
	});
	it('does not resurrect a hung-up call when credential mint resolves late', async () => {
		let resolve!: (value: { client_secret: string; model: string }) => void;
		api.mintVoiceSecret.mockReturnValue(new Promise(r => resolve = r));
		const pending = startCallForSession('late');
		await Promise.resolve();
		await hangup();
		resolve({ client_secret: 'test', model: 'test-model' });
		await pending;
		expect(capture).not.toHaveBeenCalled();
		expect(get(voiceCallState).status).toBe('ended');
	});
});
