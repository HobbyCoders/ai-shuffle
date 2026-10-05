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
 /** The microphone stopped (false) or started again (true) reaching the call (#1824). */
 micHealth?(ok: boolean): void;
 /** A fresh microphone stream, when the current track ended or went silent. */
 reacquireMic?(): Promise<MediaStream>;
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
/** Unsent microphone audio (bytes) beyond which new frames are dropped. 4 s at 24 kHz. */
export const MAX_UPLOAD_BUFFER = 192000;
/** Default microphone gate (dBFS); matches the server's `mic_threshold_db` default. */
export const DEFAULT_MIC_THRESHOLD_DB = -42;
/**
 * Microphone watchdog (#1824): an unmuted call whose microphone audio has not
 * been captured, or has not reached the server, for this long says so and
 * tries to recover instead of showing a healthy call that hears nothing.
 */
export const MIC_SILENT_MS = 4000;
/** How often the browser's call figures are sent to the server's health log. */
export const CLIENT_STATS_MS = 5000;
const WATCH_MS = 1000;
/** A watchdog tick this late means the main thread stalled; its readings are stale. */
const STALL_SKIP_MS = 500;
/** Minimum spacing between attempts to get a fresh microphone track. */
const MIC_RETRY_MS = 10000;
const GATE_BLOCK = 240; // 10 ms at 24 kHz
const GATE_PREROLL_BLOCKS = 2; // keep 20 ms before the first loud block (soft consonant onsets)
const GATE_HANGOVER = 14400; // keep 600 ms after the last loud block (word endings, short pauses)
import workletUrl from './voicePcm.worklet.js?url&no-inline';
import playbackWorkletUrl from './voicePlayback.worklet.js?url&no-inline';

/** Why the microphone is not reaching the call, or null when it is. */
export type MicProblem = 'track-ended' | 'audio-suspended' | 'no-capture' | 'not-reaching-server';

/** The watchdog's reading; pure so the rules can be tested without a browser. */
export function micProblem(s: {
 now: number; graceUntil: number; trackEnded: boolean; contextState: string;
 lastCaptureAt: number; serverMicAgeMs: number | null; serverHealthAt: number;
}): MicProblem | null {
 if (s.now < s.graceUntil) return null;
 if (s.trackEnded) return 'track-ended';
 if (s.contextState !== 'running') return 'audio-suspended';
 if (s.now - s.lastCaptureAt >= MIC_SILENT_MS) return 'no-capture';
 // Only a fresh server reading counts; an older server sends none.
 if (s.serverMicAgeMs !== null && s.now - s.serverHealthAt < 2 * MIC_SILENT_MS && s.serverMicAgeMs >= MIC_SILENT_MS) return 'not-reaching-server';
 return null;
}

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
 let player: AudioWorkletNode | null = null;
 let source: MediaStreamAudioSourceNode | null = null;
 let stopped = false;
 // Counted per stall so the console names it once, not per dropped frame.
 let droppedUpload = 0;
 let error: string | null = null;
 let endReason: VoiceStreamEndReason | null = null;
 const fail = (reason: VoiceStreamEndReason, message: string) => { endReason ??= reason; error ??= message; };
 let micMuted = false, micGeneration = 0;
 // Call health (#1824): what the browser captured, sent and played.
 const stats = { captured: 0, sent: 0, upload_dropped: 0, received: 0, underruns: 0, playback_dropped_ms: 0, depth_ms: 0, max_depth_ms: 0, target_ms: 0, stall_ms: 0, mic_recoveries: 0, device_changes: 0 };
 let lastCaptureAt = performance.now(), graceUntil = lastCaptureAt + MIC_SILENT_MS;
 let serverMicAgeMs: number | null = null, serverHealthAt = 0;
 let reconnecting = false, micOk = true, micRetryAt = 0, lastTick = performance.now();
 let watchTimer: ReturnType<typeof setInterval> | null = null, statsTimer: ReturnType<typeof setInterval> | null = null;
 const onDeviceChange = () => { stats.device_changes++; };
 const pendingText = new Map<string, { resolve: (sent: boolean) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
 const settlePending = () => {
  for (const pending of pendingText.values()) { clearTimeout(pending.timer); pending.resolve(false); }
  pendingText.clear();
 };
 const close = async () => {
  if (stopped) return;
  stopped = true;
  settlePending();
  if (watchTimer) clearInterval(watchTimer);
  if (statsTimer) clearInterval(statsTimer);
  navigator.mediaDevices?.removeEventListener?.('devicechange', onDeviceChange);
  signal.removeEventListener('abort', abort);
  socket?.close(); worklet?.disconnect(); player?.disconnect(); source?.disconnect();
  output.disconnect();
  if (context.state !== 'closed') await context.close();
 };
 const abort = () => { void close(); };
 signal.addEventListener('abort', abort, { once: true });
 if (signal.aborted) { await close(); throw new Error('Voice connection cancelled.'); }
 try {
  await context.resume();
  await Promise.all([context.audioWorklet.addModule(workletUrl), context.audioWorklet.addModule(playbackWorkletUrl)]);
  if (stopped) throw new Error('Voice connection cancelled.');
  // Speech plays on the audio thread through a jitter buffer, so a busy main
  // thread or a late frame no longer cuts the voice in and out (#1824).
  player = new AudioWorkletNode(context, 'voice-playback', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
  player.connect(output);
  player.port.onmessage = event => {
   const message = event.data;
   if (stopped || !message) return;
   if (message.type === 'speaking') {
    try { handlers.speaking(!!message.active); } catch (handlerError) { console.warn('[voice] ignoring a failed voice event handler:', handlerError); }
   } else if (message.type === 'stats') {
    stats.underruns = message.underruns; stats.playback_dropped_ms = message.droppedMs;
    stats.depth_ms = message.depthMs; stats.max_depth_ms = Math.max(stats.max_depth_ms, message.maxDepthMs); stats.target_ms = message.targetMs;
   }
  };
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
     // Handed to the audio thread as is: no per-sample work on this thread.
     stats.received++;
     player?.port.postMessage({ type: 'pcm', pcm: event.data }, [event.data]);
     return;
    }
    let message: ReturnType<typeof JSON.parse>;
    try { message = JSON.parse(event.data); }
    catch { fail('invalid-response', 'Invalid voice response.'); ws.close(); return; }
    // A failing UI handler is a client bug, not a bad server message: it must
    // never end the call (#1671).
    try {
     if (message.type === 'ready') { ready = true; settle(); resolve(); }
     else if (message.type === 'health') {
      serverMicAgeMs = typeof message.mic_age_ms === 'number' ? message.mic_age_ms : null;
      serverHealthAt = performance.now();
     }
     else if (message.type === 'text.accepted' || message.type === 'text.rejected') {
      const pending = pendingText.get(message.id);
      if (pending) {
       clearTimeout(pending.timer); pendingText.delete(message.id);
       if (message.type === 'text.accepted') pending.resolve(true);
       else pending.reject(new Error(message.message || 'The voice call could not accept your message.'));
      }
     }
     else if (message.type === 'caption') handlers.caption(message.id, message.role, message.delta);
     else if (message.type === 'status') {
      reconnecting = message.state === 'reconnecting';
      if (!reconnecting) graceUntil = performance.now() + MIC_SILENT_MS;
      handlers.reconnecting?.(reconnecting);
     }
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
  const capture = new AudioWorkletNode(context, 'voice-pcm');
  worklet = capture;
  const silent = context.createGain(); silent.gain.value = 0;
  source.connect(capture); capture.connect(silent); silent.connect(context.destination);
  let gate = createInputGate(micThresholdDb);
  capture.port.onmessage = event => {
   // Dropping frames here is independent of MediaStreamTrack.enabled. Epochs
   // reject queued pre-mute frames even after a quick mute/unmute.
   if (socket?.readyState !== WebSocket.OPEN || stopped || micMuted || event.data.generation !== micGeneration) return;
   stats.captured++; lastCaptureAt = performance.now();
   // A stalled connection is caught by its close; a burst after a stall only
   // loses the microphone audio that could not be sent in time.
   if (socket.bufferedAmount > MAX_UPLOAD_BUFFER) {
    stats.upload_dropped++;
    if (!droppedUpload++) console.info('[voice] upload fell behind; dropping microphone audio to catch up');
    return;
   }
   droppedUpload = 0;
   const input = new Int16Array(event.data.pcm);
   const gated = gate(input);
   socket.send(gated === input ? event.data.pcm : gated.buffer);
   stats.sent++;
  };

  // Microphone watchdog (#1824): an unmuted call never looks healthy while
  // its microphone audio is not reaching the call.
  const replaceMic = async (reacquire: () => Promise<MediaStream>) => {
   const next = await reacquire();
   if (stopped) return;
   source?.disconnect();
   source = context.createMediaStreamSource(next);
   source.connect(capture);
   mic = next;
  };
  const recover = (problem: MicProblem, now: number) => {
   if (problem === 'not-reaching-server') {
    // Captured but not arriving: reopen the audio connection on the same call.
    console.warn('[voice] microphone audio is not reaching the server; reconnecting the audio stream');
    if (watchTimer) clearInterval(watchTimer);
    watchTimer = null;
    fail('connection-dropped', 'The microphone stopped reaching the call.');
    ws.close(4000, 'microphone not reaching the call');
    return;
   }
   if (now < micRetryAt) return;
   micRetryAt = now + MIC_RETRY_MS;
   stats.mic_recoveries++;
   if (context.state !== 'running') void context.resume().catch(() => {});
   if (problem !== 'audio-suspended' && handlers.reacquireMic) {
    console.warn(`[voice] microphone stopped (${problem}); getting it again`);
    replaceMic(handlers.reacquireMic).catch(e => console.warn('[voice] could not get the microphone again:', e));
   }
   graceUntil = now + MIC_SILENT_MS / 2; // let the recovery take effect before judging again
  };
  watchTimer = setInterval(() => {
   const now = performance.now();
   const late = now - lastTick - WATCH_MS;
   lastTick = now;
   stats.stall_ms = Math.max(stats.stall_ms, Math.round(Math.max(0, late)));
   // After a stall, queued frames have not been handled yet; during a grace
   // period or a provider reconnect nothing is known. Keep the last reading.
   if (stopped || late > STALL_SKIP_MS || now < graceUntil || reconnecting) return;
   const track = mic.getAudioTracks?.()[0];
   const problem = micMuted || socket?.readyState !== WebSocket.OPEN ? null : micProblem({
    now, graceUntil, trackEnded: track?.readyState === 'ended', contextState: context.state,
    lastCaptureAt, serverMicAgeMs, serverHealthAt,
   });
   if ((problem === null) !== micOk) {
    micOk = problem === null;
    if (!micOk) console.warn('[voice] microphone not reaching the call:', problem);
    try { handlers.micHealth?.(micOk); } catch (handlerError) { console.warn('[voice] ignoring a failed voice event handler:', handlerError); }
   }
   if (problem) recover(problem, now);
  }, WATCH_MS);
  statsTimer = setInterval(() => {
   // Only a server that sends health reports accepts these; an older one
   // would reject the frame and end the call.
   if (stopped || socket?.readyState !== WebSocket.OPEN || !serverHealthAt) return;
   const track = mic.getAudioTracks?.()[0];
   try {
    socket.send(JSON.stringify({ type: 'client_stats', stats: {
     ...stats, muted: micMuted, mic_ok: micOk, context: context.state, rate: context.sampleRate,
     track: track?.readyState ?? 'none', capture_age_ms: Math.round(performance.now() - lastCaptureAt),
    } }));
   } catch { /* the close handler reports a dead socket */ }
   stats.stall_ms = 0; stats.max_depth_ms = 0;
  }, CLIENT_STATS_MS);
  navigator.mediaDevices?.addEventListener?.('devicechange', onDeviceChange);
  return {
   close,
   muteOutput: muted => { output.gain.value = muted ? 0 : 1; },
   muteInput: muted => {
    micMuted = muted; micGeneration++; gate = createInputGate(micThresholdDb);
    capture.port.postMessage({ type: 'mute', muted, generation: micGeneration });
    // Unmuted audio needs a moment to flow again before the watchdog judges it.
    lastCaptureAt = performance.now(); graceUntil = lastCaptureAt + MIC_SILENT_MS;
    if (muted && !micOk) {
     micOk = true;
     try { handlers.micHealth?.(true); } catch (handlerError) { console.warn('[voice] ignoring a failed voice event handler:', handlerError); }
    }
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
