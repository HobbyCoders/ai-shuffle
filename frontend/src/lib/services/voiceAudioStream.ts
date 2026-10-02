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
 closed(message: string | null): void;
}
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
 let error: string | null = null;
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
     if (nextPlay - context.currentTime > 2) {
      error = 'Voice playback fell behind. Start the call again.';
      ws.close(); return;
     }
     const node = context.createBufferSource();
     node.buffer = buffer; node.connect(output);
     nextPlay = Math.max(nextPlay, context.currentTime + .02);
     node.start(nextPlay); nextPlay += buffer.duration;
     playing.add(node); handlers.speaking(pcm.some(value => Math.abs(value) > 64));
     node.onended = () => { node.disconnect(); playing.delete(node); if (!playing.size) handlers.speaking(false); };
     return;
    }
    try {
     const message = JSON.parse(event.data);
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
     else if (message.type === 'error') { error = message.message; ws.close(); }
    } catch { error = 'Invalid voice response.'; ws.close(); }
   };
   ws.onerror = () => { error = 'The voice audio connection failed.'; };
   ws.onclose = event => {
    settle();
    settlePending();
    if (!ready) reject(new Error(error || event.reason || 'The voice provider rejected the connection.'));
    else if (!stopped) handlers.closed(error || (event.code !== 1000 ? 'The voice connection dropped.' : null));
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
   if (socket.bufferedAmount > 192000) { error = 'Voice upload fell behind. Start the call again.'; socket.close(); return; }
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
