// @ts-nocheck -- AudioWorklet globals are provided by the browser audio thread.
/**
 * Plays a streamed call's 24 kHz PCM16 on the audio thread (#1824).
 *
 * Provider speech arrives in real time, so the main thread used to schedule
 * each frame only 20 ms ahead: any late frame left a gap, and after a stall
 * the burst was thinned frame by frame, which sounded fast and garbled and
 * could lose the last words. Here a jitter buffer holds a little audio before
 * playback starts (more after each underrun), a reply always plays to its
 * last sample, and an oversized backlog is cut once, oldest audio first.
 */
const INPUT_RATE = 24000;
const START_MS = 150; // held before playback starts
const STEP_MS = 50; // added after each underrun
const MAX_TARGET_MS = 500;
const MAX_BACKLOG_MS = 8000; // a backlog beyond this is cut down to TRIM_TO_MS
const TRIM_TO_MS = 3000;
const UNDERRUN_GAP_MS = 600; // speech resuming sooner than this was an underrun, not a new reply
const SPEAKING_OFF_MS = 250;
const STATS_MS = 1000;
const CAPACITY = 1 << 18; // about 10.9 s at 24 kHz
const MASK = CAPACITY - 1;
const ms = value => Math.round(value * INPUT_RATE / 1000);

class VoicePlayback extends AudioWorkletProcessor {
 constructor() {
  super();
  this.ring = new Float32Array(CAPACITY);
  this.written = 0; // input samples ever written
  this.read = 0; // fractional read position, in input samples
  this.state = 'idle'; // idle | buffering | playing | starved
  this.waited = 0; // output frames spent buffering
  this.starved = 0; // output frames since the buffer ran dry
  this.target = ms(START_MS);
  this.speaking = false;
  this.step = INPUT_RATE / sampleRate;
  this.underruns = 0; this.droppedMs = 0; this.maxDepth = 0; this.sinceStats = 0;
  this.port.onmessage = event => { if (event.data?.type === 'pcm') this.push(new Int16Array(event.data.pcm)); };
 }
 depth() { return this.written - this.read; }
 push(pcm) {
  if (!pcm.length) return;
  if (pcm.length > CAPACITY - ms(TRIM_TO_MS)) pcm = pcm.subarray(pcm.length - (CAPACITY - ms(TRIM_TO_MS)));
  if (this.depth() + pcm.length > Math.min(ms(MAX_BACKLOG_MS), CAPACITY)) {
   // One cut of the oldest audio, never every other frame.
   const keep = Math.max(0, ms(TRIM_TO_MS) - pcm.length);
   const drop = Math.max(0, this.depth() - keep);
   this.read += drop; this.droppedMs += drop * 1000 / INPUT_RATE;
  }
  for (let i = 0; i < pcm.length; i++) this.ring[(this.written + i) & MASK] = pcm[i] / 32768;
  this.written += pcm.length;
  this.maxDepth = Math.max(this.maxDepth, this.depth());
  if (this.state === 'idle') { this.state = 'buffering'; this.waited = 0; }
  else if (this.state === 'starved') {
   if (this.starved * 1000 / sampleRate < UNDERRUN_GAP_MS) {
    this.underruns++;
    this.target = Math.min(ms(MAX_TARGET_MS), this.target + ms(STEP_MS));
   }
   this.state = 'buffering'; this.waited = 0;
  }
 }
 setSpeaking(active) {
  if (this.speaking === active) return;
  this.speaking = active;
  this.port.postMessage({ type: 'speaking', active });
 }
 process(_inputs, outputs) {
  const channels = outputs[0];
  const out = channels?.[0];
  if (!out) return true;
  for (let i = 0; i < out.length; i++) {
   if (this.state === 'buffering') {
    // Start once the buffer holds the target, or once the audio has waited
    // that long anyway (the short tail of a reply must still play).
    if (this.depth() >= this.target || this.waited * this.step >= this.target) { this.state = 'playing'; this.setSpeaking(true); }
    else { this.waited++; out[i] = 0; continue; }
   }
   if (this.state === 'playing') {
    if (this.depth() > 0) {
     const at = Math.floor(this.read), frac = this.read - at;
     const a = this.ring[at & MASK];
     const b = at + 1 < this.written ? this.ring[(at + 1) & MASK] : a;
     out[i] = a + (b - a) * frac;
     this.read += this.step;
     continue;
    }
    this.read = this.written; this.state = 'starved'; this.starved = 0;
   }
   out[i] = 0;
   if (this.state === 'starved') {
    this.starved++;
    const quiet = this.starved * 1000 / sampleRate;
    if (quiet >= SPEAKING_OFF_MS) this.setSpeaking(false);
    if (quiet >= UNDERRUN_GAP_MS) this.state = 'idle';
   }
  }
  for (let c = 1; c < channels.length; c++) channels[c].set(out);
  this.sinceStats += out.length;
  if (this.sinceStats * 1000 / sampleRate >= STATS_MS) {
   this.port.postMessage({
    type: 'stats', underruns: this.underruns, droppedMs: Math.round(this.droppedMs),
    depthMs: Math.round(this.depth() * 1000 / INPUT_RATE), maxDepthMs: Math.round(this.maxDepth * 1000 / INPUT_RATE),
    targetMs: Math.round(this.target * 1000 / INPUT_RATE),
   });
   this.sinceStats = 0; this.maxDepth = this.depth();
  }
  return true;
 }
}
registerProcessor('voice-playback', VoicePlayback);
