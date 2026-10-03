import { wsUrl } from '$lib/api/runtime';

export interface VoiceAudioStream {
 close(): Promise<void>;
 muteOutput(muted: boolean): void;
 muteInput(muted: boolean): void;
 sendText(text: string): Promise<boolean>;
}
interface Handlers {
 caption(id: string, role: 'user' | 'assistant', delta: string): void;
 speaking(active: boolean): void;
 closed(message: string | null, reason: VoiceStreamEndReason): void;
 /** The server is restoring (true) or has restored (false) its provider connection. */
 reconnecting?(active: boolean): void;
}
/**
 * Why the stream ended without the client asking it to (#1671). The labels are
 * logged and sent with the hangup, so they must stay free of call content.
 */
export type VoiceStreamEndReason =
 | 'provider-closed'
 | 'provider-error'
 | 'server-closed'
 | 'connection-dropped'
 | 'invalid-response';
/**
 * Audio already scheduled ahead of the listener, in seconds. Speech that would
 * start later than this is stale: it is dropped and the call stays up (#1671).
 * A main-thread stall (opening a large session, a busy render) holds incoming
 * frames in the socket; they then arrive together, and ending the call over
 * that burst made switching sessions mid-reply hang up.
 */
export const MAX_PLAYBACK_AHEAD_S = 2;
/** Unsent microphone audio (bytes) beyond which new frames are dropped. 4 s at 24 kHz. */
export const MAX_UPLOAD_BUFFER = 192000;
/** Default microphone gate (dBFS); matches the server's `mic_threshold_db` default. */
export const DEFAULT_MIC_THRESHOLD_DB = -42;
const GATE_BLOCK = 240; // 10 ms at 24 kHz
const GATE_PREROLL_BLOCKS = 2; // keep 20 ms before the first loud block (soft consonant onsets)
const GATE_HANGOVER = 14400; // keep 600 ms after the last loud block (word endings, short pauses)
import workletUrl from './voicePcm.worklet.js?url&no-inline';

/**
 * Microphone gate for streamed calls. GPT Live has no VAD or noise settings,
 * so breaths and room noise used to reach it as speech and start a turn.
 * Blocks quieter than the threshold are sent as silence: the provider still
 * receives continuous audio, and nothing is delayed. Returns the same array
 * when nothing was gated.
 */
export function createInputGate(thresholdDb = DEFAULT_MIC_THRESHOLD_DB): (pcm: Int16Array) => Int16Array {
 const threshold = 32768 * 10 ** (thresholdDb / 20);
 let hold = 0; // samples still open after earlier loud audio
 return pcm => {
  const blocks = Math.ceil(pcm.length / GATE_BLOCK);
  const open = new Array<boolean>(blocks).fill(false);
  for (let b = 0; b < blocks; b++) {
   const start = b * GATE_BLOCK, end = Math.min(pcm.length, start + GATE_BLOCK);
   let sum = 0;
   for (let i = start; i < end; i++) sum += pcm[i] * pcm[i];
   if (Math.sqrt(sum / (end - start)) >= threshold) {
    for (let p = Math.max(0, b - GATE_PREROLL_BLOCKS); p <= b; p++) open[p] = true;
    hold = GATE_HANGOVER;
   } else if (hold > 0) {
    open[b] = true;
    hold = Math.max(0, hold - (end - start));
   }
  }
  if (open.every(Boolean)) return pcm;
  const gated = new Int16Array(pcm.length);
  open.forEach((on, b) => { if (on) gated.set(pcm.subarray(b * GATE_BLOCK, (b + 1) * GATE_BLOCK), b * GATE_BLOCK); });
  return gated;
 };
}

/**
 * One bidirectional PCM stream; the server owns provider auth and delegation.
 * `resume` reopens a dropped call: the server replaces the session's leftover
 * audio connection instead of refusing a second one.
 */
export async function openVoiceAudioStream(sessionId: string, mic: MediaStream, handlers: Handlers, signal: AbortSignal, micThresholdDb = DEFAULT_MIC_THRESHOLD_DB, resume = false): Promise<VoiceAudioStream> {
 const context = new AudioContext();
 const output = context.createGain();
 output.connect(context.destination);
 let socket: WebSocket | null = null;
 let worklet: AudioWorkletNode | null = null;
 let source: MediaStreamAudioSourceNode | null = null;
 let stopped = false;
 let nextPlay = 0;
 // Counted per stall so the console names it once, not per dropped frame.
 let droppedPlayback = 0, droppedUpload = 0;
 let error: string | null = null;
 let endReason: VoiceStreamEndReason | null = null;
 const fail = (reason: VoiceStreamEndReason, message: string) => { endReason ??= reason; error ??= message; };
 let micMuted = false, micGeneration = 0;
 const pendingText = new Map<string, { resolve: (sent: boolean) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
 const settlePending = () => {
  for (const pending of pendingText.values()) { clearTimeout(pending.timer); pending.resolve(false); }
  pendingText.clear();
 };
 const playing = new Set<AudioBufferSourceNode>();
 const close = async () => {
  if (stopped) return;
  stopped = true;
  settlePending();
  signal.removeEventListener('abort', abort);
  socket?.close(); worklet?.disconnect(); source?.disconnect();
  for (const node of playing) { try { node.stop(); } catch { /* already ended */ } }
  playing.clear(); output.disconnect();
  if (context.state !== 'closed') await context.close();
 };
 const abort = () => { void close(); };
 signal.addEventListener('abort', abort, { once: true });
 if (signal.aborted) { await close(); throw new Error('Voice connection cancelled.'); }
 try {
  await context.resume();
  await context.audioWorklet.addModule(workletUrl);
  if (stopped) throw new Error('Voice connection cancelled.');
  socket = new WebSocket(wsUrl(`/api/v1/voice/sessions/${encodeURIComponent(sessionId)}/audio`, { resume: resume ? '1' : null }));
  socket.binaryType = 'arraybuffer';
  const ws = socket;
  await new Promise<void>((resolve, reject) => {
   let ready = false;
   const timer = setTimeout(() => reject(new Error('The voice provider did not connect in time.')), 110000);
   const cancel = () => { clearTimeout(timer); reject(new Error('Voice connection cancelled.')); };
   signal.addEventListener('abort', cancel, { once: true });
   const settle = () => { clearTimeout(timer); signal.removeEventListener('abort', cancel); };
   ws.onmessage = event => {
    if (stopped) return;
    if (event.data instanceof ArrayBuffer) {
     if (!ready || event.data.byteLength % 2) return;
     const pcm = new Int16Array(event.data);
     const buffer = context.createBuffer(1, pcm.length, 24000);
     const samples = buffer.getChannelData(0);
     for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
     if (nextPlay - context.currentTime > MAX_PLAYBACK_AHEAD_S) {
      if (!droppedPlayback++) console.info('[voice] playback fell behind; dropping stale speech to catch up');
      return;
     }
     droppedPlayback = 0;
     const node = context.createBufferSource();
     node.buffer = buffer; node.connect(output);
     nextPlay = Math.max(nextPlay, context.currentTime + .02);
     node.start(nextPlay); nextPlay += buffer.duration;
     playing.add(node); handlers.speaking(pcm.some(value => Math.abs(value) > 64));
     node.onended = () => { node.disconnect(); playing.delete(node); if (!playing.size) handlers.speaking(false); };
     return;
    }
    let message: ReturnType<typeof JSON.parse>;
    try { message = JSON.parse(event.data); }
    catch { fail('invalid-response', 'Invalid voice response.'); ws.close(); return; }
    // A failing UI handler is a client bug, not a bad server message: it must
    // never end the call (#1671).
    try {
     if (message.type === 'ready') { ready = true; settle(); resolve(); }
     else if (message.type === 'text.accepted' || message.type === 'text.rejected') {
      const pending = pendingText.get(message.id);
      if (pending) {
       clearTimeout(pending.timer); pendingText.delete(message.id);
       if (message.type === 'text.accepted') pending.resolve(true);
       else pending.reject(new Error(message.message || 'The voice call could not accept your message.'));
      }
     }
     else if (message.type === 'caption') handlers.caption(message.id, message.role, message.delta);
     else if (message.type === 'status') handlers.reconnecting?.(message.state === 'reconnecting');
     else if (message.type === 'ended') { fail('provider-closed', 'The voice provider ended the call.'); }
     else if (message.type === 'error') { fail('provider-error', message.message || 'The voice provider reported an error.'); ws.close(); }
    } catch (handlerError) { console.warn('[voice] ignoring a failed voice event handler:', handlerError); }
   };
   ws.onerror = () => { error ??= 'The voice audio connection failed.'; };
   ws.onclose = event => {
    settle();
    settlePending();
    if (!ready) reject(new Error(error || event.reason || 'The voice provider rejected the connection.'));
    else if (!stopped) {
     if (event.code !== 1000) fail('connection-dropped', 'The voice connection dropped.');
     else fail('server-closed', 'The voice server ended the call.');
     handlers.closed(error, endReason ?? 'server-closed');
    }
   };
  });
  if (stopped) throw new Error('Voice connection cancelled.');
  source = context.createMediaStreamSource(mic);
  worklet = new AudioWorkletNode(context, 'voice-pcm');
  const silent = context.createGain(); silent.gain.value = 0;
  source.connect(worklet); worklet.connect(silent); silent.connect(context.destination);
  let gate = createInputGate(micThresholdDb);
  worklet.port.onmessage = event => {
   // Dropping frames here is independent of MediaStreamTrack.enabled. Epochs
   // reject queued pre-mute frames even after a quick mute/unmute.
   if (socket?.readyState !== WebSocket.OPEN || stopped || micMuted || event.data.generation !== micGeneration) return;
   // A stalled connection is caught by its close; a burst after a stall only
   // loses the microphone audio that could not be sent in time.
   if (socket.bufferedAmount > MAX_UPLOAD_BUFFER) {
    if (!droppedUpload++) console.info('[voice] upload fell behind; dropping microphone audio to catch up');
    return;
   }
   droppedUpload = 0;
   const input = new Int16Array(event.data.pcm);
   const gated = gate(input);
   socket.send(gated === input ? event.data.pcm : gated.buffer);
  };
  return {
   close,
   muteOutput: muted => { output.gain.value = muted ? 0 : 1; },
   muteInput: muted => {
    micMuted = muted; micGeneration++; gate = createInputGate(micThresholdDb);
    worklet?.port.postMessage({ type: 'mute', muted, generation: micGeneration });
   },
   sendText: text => {
    if (stopped || socket?.readyState !== WebSocket.OPEN) return Promise.resolve(false);
    if (!text.trim() || new TextEncoder().encode(text).length > 16000) return Promise.reject(new Error('Voice messages must contain text and be at most 16,000 bytes. Your draft has been kept.'));
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
     const timer = setTimeout(() => {
      pendingText.delete(id);
      reject(new Error('Could not confirm message delivery. Check the conversation before retrying.'));
     }, 10000);
     pendingText.set(id, { resolve, reject, timer });
     try { socket!.send(JSON.stringify({ type: 'input_text', id, text })); }
     catch { clearTimeout(timer); pendingText.delete(id); resolve(false); }
    });
   },
  };
 } catch (e) { await close(); throw e; }
}
