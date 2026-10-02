import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
const api = vi.hoisted(() => ({ prepareConversationCall: vi.fn(), mintVoiceSecret: vi.fn(), attachVoiceCall: vi.fn(), hangupVoiceCall: vi.fn(), getVoiceCallState: vi.fn() }));
vi.mock('$lib/api/voice', () => api);
const streamed = vi.hoisted(() => ({ close: vi.fn(), muteInput: vi.fn(), muteOutput: vi.fn(), sendText: vi.fn() }));
vi.mock('./voiceAudioStream', () => ({ openVoiceAudioStream: vi.fn(async () => streamed) }));
import { startCall, startCallForSession, hangup, voiceCallState, sendVoiceText, reconcileVoiceCall, confirmCallReplacement, toggleMic } from './voiceCall';

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
		expect(api.hangupVoiceCall).toHaveBeenCalledWith('owner');
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
