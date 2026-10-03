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
import workletUrl from './voicePcm.worklet.js?url&no-inline';

/** One bidirectional PCM stream; the server owns provider auth and delegation. */
export async function openVoiceAudioStream(sessionId: string, mic: MediaStream, handlers: Handlers, signal: AbortSignal): Promise<VoiceAudioStream> {
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
  socket = new WebSocket(wsUrl(`/api/v1/voice/sessions/${encodeURIComponent(sessionId)}/audio`));
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
   socket.send(event.data.pcm);
  };
  return {
   close,
   muteOutput: muted => { output.gain.value = muted ? 0 : 1; },
   muteInput: muted => {
    micMuted = muted; micGeneration++;
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
