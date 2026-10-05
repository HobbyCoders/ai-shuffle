import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('./voicePcm.worklet.js?url&no-inline', () => ({ default: '/voice-pcm.js' }));
vi.mock('./voicePlayback.worklet.js?url&no-inline', () => ({ default: '/voice-playback.js' }));
vi.mock('$lib/api/runtime', () => ({
 wsUrl: (path: string, params: Record<string, string | null> = {}) => {
  const query = new URLSearchParams(Object.entries(params).filter((entry): entry is [string, string] => entry[1] != null)).toString();
  return 'ws://localhost' + path + (query ? '?' + query : '');
 },
}));
import { CLIENT_STATS_MS, DEFAULT_MIC_THRESHOLD_DB, MAX_UPLOAD_BUFFER, MIC_SILENT_MS, createInputGate, micProblem, openVoiceAudioStream } from './voiceAudioStream';

const nodes: any[] = [];
const contexts: any[] = [];
const sockets: any[] = [];
class Context {
 state = 'running'; currentTime = 0; destination = {}; sampleRate = 48000;
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
 constructor(_context: unknown, public name: string) { nodes.push(this); }
}
/** The microphone capture node and the speech playback node. */
const capture = () => nodes.find(n => n.name === 'voice-pcm');
const playback = () => nodes.find(n => n.name === 'voice-playback');
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
 it('passes on the server status while it restores the provider connection', async () => {
  const h = { ...handlers(), reconnecting: vi.fn() };
  const stream = await openVoiceAudioStream('owner', {} as MediaStream, h, new AbortController().signal);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'status', state: 'reconnecting' }) });
  sockets[0].onmessage({ data: JSON.stringify({ type: 'status', state: 'live' }) });
  expect(h.reconnecting.mock.calls).toEqual([[true], [false]]);
  expect(h.closed).not.toHaveBeenCalled();
  await stream.close();
 });
 it('asks the server to replace a leftover connection only when reopening a dropped call', async () => {
  const first = await openVoiceAudioStream('owner', {} as MediaStream, handlers(), new AbortController().signal);
  const reopened = await openVoiceAudioStream('owner', {} as MediaStream, handlers(), new AbortController().signal, undefined, true);
  expect(sockets[0].url).toBe('ws://localhost/api/v1/voice/sessions/owner/audio');
  expect(sockets[1].url).toBe('ws://localhost/api/v1/voice/sessions/owner/audio?resume=1');
  await first.close(); await reopened.close();
 });
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
  capture().port.onmessage({ data: { pcm, generation: 0 } });
  capture().port.onmessage({ data: { pcm, generation: 1 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  const text = stream.sendText('Typed while muted');
  const event = JSON.parse(sockets[0].send.mock.calls[0][0]);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'text.accepted', id: event.id }) });
  expect(await text).toBe(true);
  sockets[0].send.mockClear();
  stream.muteInput(false);
  capture().port.onmessage({ data: { pcm, generation: 0 } });
  capture().port.onmessage({ data: { pcm, generation: 1 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  capture().port.onmessage({ data: { pcm, generation: 2 } });
  expect(sockets[0].send).toHaveBeenCalledExactlyOnceWith(pcm);
  expect(capture().port.postMessage.mock.calls.map(([m]: any[]) => m)).toEqual([
   { type: 'mute', muted: true, generation: 1 }, { type: 'mute', muted: false, generation: 2 }
  ]);
  await stream.close();
 });

 it('starts only after provider readiness, sends PCM and receives captions/audio', async () => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('session-1', {} as MediaStream, callbacks, new AbortController().signal);
  expect(sockets[0].url).toBe('ws://localhost/api/v1/voice/sessions/session-1/audio');
  expect(contexts[0].audioWorklet.addModule).toHaveBeenCalledWith('/voice-pcm.js');
  expect(contexts[0].audioWorklet.addModule).toHaveBeenCalledWith('/voice-playback.js');
  const pcm = new Int16Array([3000, -3000]).buffer; // speech level, above the microphone gate
  capture().port.onmessage({ data: { pcm, generation: 0 } });
  expect(sockets[0].send).toHaveBeenCalledWith(pcm);
  sockets[0].onmessage({ data: JSON.stringify({ type: 'caption', id: 'u1', role: 'user', delta: 'Hi' }) });
  expect(callbacks.caption).toHaveBeenCalledWith('u1', 'user', 'Hi');
  // Speech goes to the audio thread untouched; it reports when it plays.
  const speech = new Int16Array([3000, -3000]).buffer;
  sockets[0].onmessage({ data: speech });
  expect(playback().port.postMessage).toHaveBeenCalledWith({ type: 'pcm', pcm: speech }, [speech]);
  playback().port.onmessage({ data: { type: 'speaking', active: true } });
  expect(callbacks.speaking).toHaveBeenLastCalledWith(true);
  playback().port.onmessage({ data: { type: 'speaking', active: false } });
  expect(callbacks.speaking).toHaveBeenLastCalledWith(false);
  await stream.close();
  expect(contexts[0].close).toHaveBeenCalledOnce();
  expect(capture().disconnect).toHaveBeenCalledOnce();
  expect(playback().disconnect).toHaveBeenCalledOnce();
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
  for (let i = 0; i < 3; i++) capture().port.onmessage({ data: { pcm: new Int16Array(1920).buffer, generation: 0 } });
  expect(sockets[0].send).not.toHaveBeenCalled();
  expect(info).toHaveBeenCalledOnce();
  // Once the socket drains, the microphone flows again on the same call.
  sockets[0].bufferedAmount = 0;
  capture().port.onmessage({ data: { pcm: new Int16Array(1920).buffer, generation: 0 } });
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
 // #1671/#1824: switching sessions mid-reply stalls the main thread while the
 // agent keeps talking. The frames held in the socket then arrive together.
 // They used to be thinned frame by frame (fast, garbled speech, lost final
 // words); now every frame reaches the playback buffer in order.
 it('passes a burst of speech after a main-thread stall to playback whole and in order', async () => {
  const callbacks = handlers();
  const stream = await openVoiceAudioStream('s', {} as MediaStream, callbacks, new AbortController().signal);
  const frames = Array.from({ length: 50 }, (_, i) => new Int16Array(2400).fill(i + 1).buffer); // 5 s
  for (const data of frames) sockets[0].onmessage({ data });
  const sent = playback().port.postMessage.mock.calls.map(([m]: any[]) => m.pcm);
  expect(sent).toEqual(frames);
  expect(callbacks.closed).not.toHaveBeenCalled();
  expect(sockets[0].close).not.toHaveBeenCalled();
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
  capture().port.onmessage({ data: { pcm: breath.buffer, generation: 0 } });
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

// #1824: a call that stops hearing its caller says so and tries to recover.
describe('microphone watchdog', () => {
 const fakeTime = () => vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance', 'Date'] });
 const micStream = (state = 'live') => ({ getAudioTracks: () => [{ readyState: state }] }) as unknown as MediaStream;
 const speak = () => capture().port.onmessage({ data: { pcm: new Int16Array(1920).fill(3000).buffer, generation: 0 } });
 afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
 const quiet = () => { vi.spyOn(console, 'warn').mockImplementation(() => {}); vi.spyOn(console, 'info').mockImplementation(() => {}); };

 it('reads the microphone state from capture, track, audio context and server', () => {
  const base = { now: 10_000, graceUntil: 0, trackEnded: false, contextState: 'running', lastCaptureAt: 9_900, serverMicAgeMs: 100, serverHealthAt: 9_500 };
  expect(micProblem(base)).toBeNull();
  expect(micProblem({ ...base, graceUntil: 20_000, trackEnded: true })).toBeNull();
  expect(micProblem({ ...base, trackEnded: true })).toBe('track-ended');
  expect(micProblem({ ...base, contextState: 'suspended' })).toBe('audio-suspended');
  expect(micProblem({ ...base, lastCaptureAt: 10_000 - MIC_SILENT_MS })).toBe('no-capture');
  expect(micProblem({ ...base, serverMicAgeMs: MIC_SILENT_MS })).toBe('not-reaching-server');
  // An old reading, or a server that sends none, is never taken as a problem.
  expect(micProblem({ ...base, serverMicAgeMs: MIC_SILENT_MS, serverHealthAt: 0 })).toBeNull();
  expect(micProblem({ ...base, serverMicAgeMs: null })).toBeNull();
 });

 it('shows the problem when no microphone audio is captured, gets the microphone again and clears', async () => {
  fakeTime(); quiet();
  const fresh = micStream();
  const h = { ...handlers(), micHealth: vi.fn(), reacquireMic: vi.fn(async () => fresh) };
  const stream = await openVoiceAudioStream('s', micStream(), h, new AbortController().signal);
  await vi.advanceTimersByTimeAsync(MIC_SILENT_MS + 1000);
  expect(h.micHealth).toHaveBeenLastCalledWith(false);
  expect(h.reacquireMic).toHaveBeenCalledOnce();
  for (let i = 0; i < 4; i++) { speak(); await vi.advanceTimersByTimeAsync(1000); }
  expect(h.micHealth).toHaveBeenLastCalledWith(true);
  expect(h.closed).not.toHaveBeenCalled();
  await stream.close();
 });

 it('reconnects the audio stream when captured audio stops reaching the server', async () => {
  fakeTime(); quiet();
  const h = { ...handlers(), micHealth: vi.fn() };
  await openVoiceAudioStream('s', micStream(), h, new AbortController().signal);
  for (let i = 0; i < 6; i++) {
   speak();
   sockets[0].onmessage({ data: JSON.stringify({ type: 'health', mic_age_ms: 6000 + i * 1000, mic_frames: 10 }) });
   await vi.advanceTimersByTimeAsync(1000);
  }
  expect(h.micHealth).toHaveBeenCalledWith(false);
  expect(sockets[0].close).toHaveBeenCalledWith(4000, 'microphone not reaching the call');
  expect(h.closed).toHaveBeenCalledExactlyOnceWith('The microphone stopped reaching the call.', 'connection-dropped');
 });

 it('resumes a suspended audio context without replacing the microphone', async () => {
  fakeTime(); quiet();
  const h = { ...handlers(), micHealth: vi.fn(), reacquireMic: vi.fn(async () => micStream()) };
  const stream = await openVoiceAudioStream('s', micStream(), h, new AbortController().signal);
  contexts[0].resume.mockClear();
  contexts[0].state = 'suspended';
  await vi.advanceTimersByTimeAsync(MIC_SILENT_MS + 1000);
  expect(h.micHealth).toHaveBeenLastCalledWith(false);
  expect(contexts[0].resume).toHaveBeenCalled();
  expect(h.reacquireMic).not.toHaveBeenCalled();
  await stream.close();
 });

 it('stays quiet while the caller is muted', async () => {
  fakeTime(); quiet();
  const h = { ...handlers(), micHealth: vi.fn(), reacquireMic: vi.fn(async () => micStream()) };
  const stream = await openVoiceAudioStream('s', micStream('ended'), h, new AbortController().signal);
  stream.muteInput(true);
  await vi.advanceTimersByTimeAsync(3 * MIC_SILENT_MS);
  expect(h.micHealth).not.toHaveBeenCalled();
  expect(h.reacquireMic).not.toHaveBeenCalled();
  await stream.close();
 });

 it('reports the call figures to the server for its health log', async () => {
  fakeTime(); quiet();
  const stream = await openVoiceAudioStream('s', micStream(), handlers(), new AbortController().signal);
  await vi.advanceTimersByTimeAsync(CLIENT_STATS_MS);
  expect(sockets[0].send).not.toHaveBeenCalled(); // An older server would reject the report.
  sockets[0].onmessage({ data: JSON.stringify({ type: 'health', mic_age_ms: 50, mic_frames: 1 }) });
  speak();
  playback().port.onmessage({ data: { type: 'stats', underruns: 2, droppedMs: 0, depthMs: 140, maxDepthMs: 300, targetMs: 200 } });
  await vi.advanceTimersByTimeAsync(CLIENT_STATS_MS);
  const reports = sockets[0].send.mock.calls.map(([m]: any[]) => m).filter((m: unknown) => typeof m === 'string').map((m: string) => JSON.parse(m));
  expect(reports).toHaveLength(1);
  expect(reports[0]).toMatchObject({ type: 'client_stats', stats: {
   captured: 1, sent: 1, underruns: 2, depth_ms: 140, max_depth_ms: 300, target_ms: 200, muted: false, context: 'running', rate: 48000, track: 'live',
  } });
  await stream.close();
 });
});
