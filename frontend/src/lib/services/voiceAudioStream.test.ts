import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('./voicePcm.worklet.js?url&no-inline', () => ({ default: '/voice-pcm.js' }));
vi.mock('$lib/api/runtime', () => ({ wsUrl: (path: string) => 'ws://localhost' + path }));
import { DEFAULT_MIC_THRESHOLD_DB, MAX_PLAYBACK_AHEAD_S, MAX_UPLOAD_BUFFER, createInputGate, openVoiceAudioStream } from './voiceAudioStream';

const nodes: any[] = [];
const contexts: any[] = [];
const sockets: any[] = [];
class Context {
 state = 'running'; currentTime = 0; destination = {};
 audioWorklet = { addModule: vi.fn(async () => {}) };
 resume = vi.fn(async () => {});
 close = vi.fn(async () => { this.state = 'closed'; });
 constructor() { contexts.push(this); }
 createGain() { return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }; }
 createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
 createBuffer(_channels: number, length: number) { const samples = new Float32Array(length); return { getChannelData: () => samples, duration: length / 24000 }; }
 createBufferSource() { return { connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), buffer: null, onended: null }; }
}
class Worklet {
 port: any = { postMessage: vi.fn() }; disconnect = vi.fn(); connect = vi.fn();
 constructor() { nodes.push(this); }
}
class Socket {
 static OPEN = 1;
 readyState = 1; bufferedAmount = 0; binaryType = '';
 onmessage: any; onclose: any; onerror: any;
 send = vi.fn();
 close = vi.fn(() => this.onclose?.({ code: 1000 }));
 constructor(public url: string) {
  sockets.push(this);
  queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }));
 }
}
beforeEach(() => {
 nodes.length = contexts.length = sockets.length = 0;
 vi.stubGlobal('AudioContext', Context);
 vi.stubGlobal('AudioWorkletNode', Worklet);
 vi.stubGlobal('WebSocket', Socket);
});
afterEach(() => vi.unstubAllGlobals());
const handlers = () => ({ caption: vi.fn(), speaking: vi.fn(), closed: vi.fn() });

describe('voice PCM stream', () => {
 it('admits typed attachment references only after the server acknowledges them', async () => {
  const stream = await openVoiceAudioStream('owner', {} as MediaStream, handlers(), new AbortController().signal);
  const sent = stream.sendText('Inspect /workspace/uploads/report.pdf');
  const event = JSON.parse(sockets[0].send.mock.calls[0][0]);
  expect(event).toMatchObject({ type: 'input_text', text: 'Inspect /workspace/uploads/report.pdf' });
  let settled = false; void sent.then(() => { settled = true; });
  await Promise.resolve(); expect(settled).toBe(false);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'text.accepted', id: event.id }) });
  expect(await sent).toBe(true);
  await stream.close();
 });
 it('keeps rejected messages and pending messages on disconnect', async () => {
  const stream = await openVoiceAudioStream('owner', {} as MediaStream, handlers(), new AbortController().signal);
  const rejected = stream.sendText('Keep this');
  const event = JSON.parse(sockets[0].send.mock.calls[0][0]);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'text.rejected', id: event.id, message: 'Backend busy. Keep your draft.' }) });
  await expect(rejected).rejects.toThrow('Backend busy');
  const pending = stream.sendText('Still here');
  await stream.close(); expect(await pending).toBe(false);
 });
 it('drops queued speech during mute and across a fast unmute, while text remains usable', async () => {
  const stream = await openVoiceAudioStream('owner', {} as MediaStream, handlers(), new AbortController().signal);
  const pcm = new Int16Array([12000]).buffer;
  stream.muteInput(true);
  nodes[0].port.onmessage({ data: { pcm, generation: 0 } });
  nodes[0].port.onmessage({ data: { pcm, generation: 1 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  const text = stream.sendText('Typed while muted');
  const event = JSON.parse(sockets[0].send.mock.calls[0][0]);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'text.accepted', id: event.id }) });
  expect(await text).toBe(true);
  sockets[0].send.mockClear();
  stream.muteInput(false);
  nodes[0].port.onmessage({ data: { pcm, generation: 0 } });
  nodes[0].port.onmessage({ data: { pcm, generation: 1 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  nodes[0].port.onmessage({ data: { pcm, generation: 2 } });
  expect(sockets[0].send).toHaveBeenCalledExactlyOnceWith(pcm);
  expect(nodes[0].port.postMessage.mock.calls.map(([m]: any[]) => m)).toEqual([
   { type: 'mute', muted: true, generation: 1 }, { type: 'mute', muted: false, generation: 2 }
  ]);
  await stream.close();
 });

 it('starts only after provider readiness, sends PCM and receives captions/audio', async () => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('session-1', {} as MediaStream, callbacks, new AbortController().signal);
  expect(sockets[0].url).toBe('ws://localhost/api/v1/voice/sessions/session-1/audio');
  expect(contexts[0].audioWorklet.addModule).toHaveBeenCalledWith('/voice-pcm.js');
  const pcm = new Int16Array([3000, -3000]).buffer; // speech level, above the microphone gate
  nodes[0].port.onmessage({ data: { pcm, generation: 0 } });
  expect(sockets[0].send).toHaveBeenCalledWith(pcm);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'caption', id: 'u1', role: 'user', delta: 'Hi' }) });
  expect(callbacks.caption).toHaveBeenCalledWith('u1', 'user', 'Hi');
  sockets[0].onmessage({ data: pcm });
  expect(callbacks.speaking).toHaveBeenLastCalledWith(true);
  sockets[0].onmessage({ data: new Int16Array(4).buffer });
  expect(callbacks.speaking).toHaveBeenLastCalledWith(false);
  await stream.close();
  expect(contexts[0].close).toHaveBeenCalledOnce();
  expect(nodes[0].disconnect).toHaveBeenCalledOnce();
 });
 it('closes the audio graph when a connecting or live call is cancelled', async () => {
  const controller = new AbortController();
  await openVoiceAudioStream('s', {} as MediaStream, handlers(), controller.signal);
  controller.abort();
  await vi.waitFor(() => expect(contexts[0].close).toHaveBeenCalledOnce());
  expect(sockets[0].close).toHaveBeenCalledOnce();
 });
 it('does not acquire a socket for a cancelled start', async () => {
  const controller = new AbortController(); controller.abort();
  await expect(openVoiceAudioStream('s', {} as MediaStream, handlers(), controller.signal)).rejects.toThrow('cancelled');
  expect(sockets).toHaveLength(0);
 });
 it('drops stale microphone audio behind a stalled upload and keeps the call up', async () => {
  const callbacks = handlers();
  const info = vi.spyOn(console, 'info').mockImplementation(() => {});
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  sockets[0].bufferedAmount = MAX_UPLOAD_BUFFER + 1;
  for (let i = 0; i < 3; i++) nodes[0].port.onmessage({ data: { pcm: new Int16Array(1920).buffer, generation: 0 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  expect(info).toHaveBeenCalledOnce();
  // Once the socket drains, the microphone flows again on the same call.
  sockets[0].bufferedAmount = 0;
  nodes[0].port.onmessage({ data: { pcm: new Int16Array(1920).buffer, generation: 0 } });
  expect(sockets[0].send).toHaveBeenCalledOnce();
  expect(sockets[0].close).not.toHaveBeenCalled();
  expect(callbacks.closed).not.toHaveBeenCalled();
  info.mockRestore();
  await stream.close();
 });
 // #1671: every unrequested end names its cause.
 it('names a provider that ended the session', async () => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'ended', reason: 'provider-closed' }) });
  sockets[0].onclose({ code: 1000 });
  expect(callbacks.closed).toHaveBeenCalledExactlyOnceWith('The voice provider ended the call.', 'provider-closed');
  await stream.close();
 });
 it.each([
  [1000, 'The voice server ended the call.', 'server-closed'],
  [1006, 'The voice connection dropped.', 'connection-dropped'],
 ])('never reports an unexplained close (code %i) as a silent end', async (code, message, reason) => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  sockets[0].onclose({ code });
  expect(callbacks.closed).toHaveBeenCalledExactlyOnceWith(message, reason);
  await stream.close();
 });
 it('names a provider error', async () => {
  const errored = handlers();
  const first = await openVoiceAudioStream('s', {} as MediaStream, errored, new AbortController().signal);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'error', message: 'Session expired.' }) });
  expect(errored.closed).toHaveBeenCalledWith('Session expired.', 'provider-error');
  await first.close();
 });
 // #1671: switching sessions mid-reply stalls the main thread while the agent
 // keeps talking. The frames held in the socket then arrive together; that
 // burst used to end the call as a playback backlog.
 it('keeps the call up through a burst of speech after a main-thread stall', async () => {
  const callbacks = handlers();
  const info = vi.spyOn(console, 'info').mockImplementation(() => {});
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  const frame = () => sockets[0].onmessage({ data: new Int16Array(2400).buffer }); // 100 ms
  const scheduled: number[] = [];
  contexts[0].createBufferSource = () => ({ connect: vi.fn(), disconnect: vi.fn(), stop: vi.fn(), buffer: null, onended: null,
   start: (at: number) => scheduled.push(at) });
  for (let i = 0; i < 50; i++) frame(); // 5 s of speech delivered at once
  expect(callbacks.closed).not.toHaveBeenCalled();
  expect(sockets[0].close).not.toHaveBeenCalled();
  // Only about MAX_PLAYBACK_AHEAD_S of it is queued; the stale rest is dropped.
  const ahead = scheduled.length * 0.1;
  expect(ahead).toBeGreaterThanOrEqual(MAX_PLAYBACK_AHEAD_S);
  expect(ahead).toBeLessThanOrEqual(MAX_PLAYBACK_AHEAD_S + 0.2);
  expect(info).toHaveBeenCalledOnce();
  // Playback catches up, and new speech is scheduled again on the same call.
  contexts[0].currentTime = 10;
  frame();
  expect(scheduled.at(-1)).toBeCloseTo(10.02);
  info.mockRestore();
  await stream.close();
 });
 it('keeps the call up when a UI handler throws on a valid event', async () => {
  const callbacks = handlers();
  callbacks.caption.mockImplementation(() => { throw new Error('unmounted view'); });
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'caption', id: 'a1', role: 'assistant', delta: 'Hi' }) });
  expect(sockets[0].close).not.toHaveBeenCalled();
  expect(callbacks.closed).not.toHaveBeenCalled();
  expect(warn).toHaveBeenCalled();
  sockets[0].onmessage({ data: 'not json' });
  expect(callbacks.closed).toHaveBeenCalledWith('Invalid voice response.', 'invalid-response');
  warn.mockRestore();
  await stream.close();
 });
 it('sends quiet microphone input as silence without dropping frames', async () => {
  const stream = await openVoiceAudioStream('s', {} as MediaStream, handlers(), new AbortController().signal, -42);
  const breath = new Int16Array(1920).fill(120); // about -49 dBFS
  nodes[0].port.onmessage({ data: { pcm: breath.buffer, generation: 0 } });
  const sent = new Int16Array(sockets[0].send.mock.calls[0][0]);
  expect(sent).toHaveLength(1920); // continuous audio: the provider still hears time pass
  expect(sent.every(v => v === 0)).toBe(true);
  await stream.close();
 });
});

describe('microphone gate', () => {
 const frame = (value: number, length = 1920) => new Int16Array(length).fill(value);
 const silent = (pcm: Int16Array) => pcm.every(v => v === 0);
 it('passes speech unchanged and silences breath-level noise', () => {
  const gate = createInputGate(DEFAULT_MIC_THRESHOLD_DB);
  expect(silent(gate(frame(120)))).toBe(true);
  const speech = frame(3000);
  expect(gate(speech)).toBe(speech);
 });
 it('keeps 600 ms after speech so word endings and short pauses are not clipped', () => {
  const gate = createInputGate(-42);
  gate(frame(3000));
  // 7 quiet 80 ms frames (560 ms) stay open, the 600 ms hold ends in the 8th.
  for (let i = 0; i < 7; i++) expect(silent(gate(frame(50)))).toBe(false);
  const eighth = gate(frame(50));
  expect(eighth.subarray(0, 960).every(v => v === 50)).toBe(true);
  expect(silent(eighth.subarray(960))).toBe(true);
  expect(silent(gate(frame(50)))).toBe(true);
 });
 it('opens 20 ms before the first loud block inside a frame', () => {
  const gate = createInputGate(-42);
  const pcm = frame(50);
  pcm.fill(3000, 1200); // speech starts at block 5 (50 ms)
  const out = gate(pcm);
  expect(silent(out.subarray(0, 720))).toBe(true);
  expect(out.subarray(720, 1200).every(v => v === 50)).toBe(true); // soft onset kept
  expect(out.subarray(1200).every(v => v === 3000)).toBe(true);
 });
 it('honours a lower threshold for quiet microphones', () => {
  expect(silent(createInputGate(-60)(frame(120)))).toBe(false);
 });
});
