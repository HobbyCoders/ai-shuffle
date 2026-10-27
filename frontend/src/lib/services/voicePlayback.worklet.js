// @ts-nocheck -- AudioWorklet globals are provided by the browser audio thread.
/**
 * Plays a streamed call's 24 kHz PCM16 on the audio thread (#1824, #731).
 *
 * Provider speech arrives in one of two patterns:
 *
 * - In real time (GPT Live, ChatGPT): each frame lands about when it should
 *   play. The main thread used to schedule each frame only 20 ms ahead, so any
 *   late frame left a gap, and after a stall the burst was thinned frame by
 *   frame, which sounded fast and garbled and could lose the last words. Here a
 *   jitter buffer holds a little audio before playback starts (more after each
 *   underrun), and a reply always plays to its last sample.
 * - Faster than real time (Grok): the provider sends a reply as a burst, say
 *   27 s of speech in 5 to 19 s. That backlog is the reply itself, not
 *   latency, so it is kept and played in full, in order, at normal speed. The
 *   ring holds about 87 s for this.
 *
 * Only a late backlog is cut: audio held up by a main-thread stall, a network
 * hiccup or a suspended page and then released at once. The player tells the
 * two apart by comparing the audio received since the reply started (or last
 * ran dry) with the time since then. Audio ahead of that clock arrived early
 * and is never cut. The credit is capped at MAX_BURST_RATE times real time, so
 * a clump released in an instant after a gap counts as late. A late backlog
 * over MAX_BACKLOG_MS is cut once, oldest audio first, down to TRIM_TO_MS. A
 * backlog that would overflow the ring is cut the same way, so a runaway
 * stream stays bounded. Barge-in (`clear`) still empties the buffer at once.
 */
const INPUT_RATE = 24000;
const START_MS = 150; // held before playback starts
const STEP_MS = 50; // added after each underrun
const MAX_TARGET_MS = 500;
const MAX_BACKLOG_MS = 8000; // a late backlog beyond this is cut down to TRIM_TO_MS
const TRIM_TO_MS = 3000;
// A provider streams at most this many times real time; audio delivered
// faster than that was held up somewhere and counts as late.
const MAX_BURST_RATE = 16;
const UNDERRUN_GAP_MS = 600; // speech resuming sooner than this was an underrun, not a new reply
const SPEAKING_OFF_MS = 250;
const STATS_MS = 1000;
const CAPACITY = 1 << 21; // about 87 s at 24 kHz (8 MB)
const MASK = CAPACITY - 1;
const ms = value => Math.round(value * INPUT_RATE / 1000);
const MAX_DEPTH = CAPACITY - ms(1000); // hard cap, with room for the frame being written

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
  this.clock = 0; // output frames rendered, as input samples: the audio thread's own clock
  this.replyAt = 0; // clock when the current reply's first audio arrived
  this.replyReceived = 0; // input samples the current reply has received
  this.underruns = 0; this.droppedMs = 0; this.maxDepth = 0; this.maxLead = 0; this.sinceStats = 0;
  this.port.onmessage = event => {
   if (event.data?.type === 'pcm') this.push(new Int16Array(event.data.pcm));
   else if (event.data?.type === 'clear') {
    this.read = this.written; this.state = 'idle'; this.waited = 0; this.starved = 0;
    this.speaking = false; this.port.postMessage({ type: 'speaking', active: false });
   }
  };
 }
 depth() { return this.written - this.read; }
 /** How far the current reply's audio arrived ahead of real time (input samples), never negative. */
 lead() {
  const elapsed = this.clock - this.replyAt;
  return Math.max(0, Math.min(this.replyReceived - elapsed, (MAX_BURST_RATE - 1) * elapsed));
 }
 push(pcm) {
  if (!pcm.length) return;
  if (pcm.length > MAX_DEPTH) pcm = pcm.subarray(pcm.length - MAX_DEPTH);
  // The clock restarts with each reply and each time playback ran dry: a
  // clump that arrives after a gap is judged on its own pace.
  if (this.state === 'idle' || this.state === 'starved') { this.replyAt = this.clock; this.replyReceived = 0; }
  this.replyReceived += pcm.length;
  const lead = this.lead();
  this.maxLead = Math.max(this.maxLead, lead);
  const depth = this.depth() + pcm.length;
  if (depth - lead > ms(MAX_BACKLOG_MS) || depth > MAX_DEPTH) {
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
  this.clock += out.length * this.step;
  this.sinceStats += out.length;
  if (this.sinceStats * 1000 / sampleRate >= STATS_MS) {
   this.port.postMessage({
    type: 'stats', underruns: this.underruns, droppedMs: Math.round(this.droppedMs),
    depthMs: Math.round(this.depth() * 1000 / INPUT_RATE), maxDepthMs: Math.round(this.maxDepth * 1000 / INPUT_RATE),
    targetMs: Math.round(this.target * 1000 / INPUT_RATE), maxLeadMs: Math.round(this.maxLead * 1000 / INPUT_RATE),
   });
   this.sinceStats = 0; this.maxDepth = this.depth(); this.maxLead = this.lead();
  }
  return true;
 }
}
registerProcessor('voice-playback', VoicePlayback);
