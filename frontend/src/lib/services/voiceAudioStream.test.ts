import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('./voicePcm.worklet.js?url&no-inline', () => ({ default: '/voice-pcm.js' }));
vi.mock('$lib/api/runtime', () => ({ wsUrl: (path: string) => 'ws://localhost' + path }));
import { openVoiceAudioStream } from './voiceAudioStream';

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
  const pcm = new Int16Array([100, -100]).buffer;
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
 it('does not queue stale microphone audio behind a stalled upload', async () => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  sockets[0].bufferedAmount = 200000;
  nodes[0].port.onmessage({ data: { pcm: new Int16Array(1920).buffer, generation: 0 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  expect(callbacks.closed).toHaveBeenCalledWith('Voice upload fell behind. Start the call again.');
  await stream.close();
 });
});
