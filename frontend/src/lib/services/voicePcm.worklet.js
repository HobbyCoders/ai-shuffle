// @ts-nocheck -- AudioWorklet globals are provided by the browser audio thread.
class VoicePcm extends AudioWorkletProcessor {
 constructor() {
  super(); this.data = new Int16Array(1920); this.n = 0; this.phase = 0; this.sum = 0; this.count = 0;
  this.muted = false; this.generation = 0;
  this.port.onmessage = event => {
   if (event.data?.type !== 'mute') return;
   this.muted = event.data.muted; this.generation = event.data.generation;
   // Never resume a partial frame containing speech captured before mute.
   this.data = new Int16Array(1920); this.n = 0; this.phase = 0; this.sum = 0; this.count = 0;
  };
 }
 process(inputs) {
  const input = inputs[0]?.[0];
  if (!input || this.muted) return true;
  for (const sample of input) {
   this.sum += sample; this.count++; this.phase += 24000;
   if (this.phase >= sampleRate) {
    this.phase -= sampleRate;
    const value = Math.max(-1, Math.min(1, this.sum / this.count));
    this.data[this.n++] = value < 0 ? value * 32768 : value * 32767;
    this.sum = 0; this.count = 0;
    if (this.n === this.data.length) {
     this.port.postMessage({ pcm: this.data.buffer, generation: this.generation }, [this.data.buffer]);
     this.data = new Int16Array(1920); this.n = 0;
    }
   }
  }
  return true;
 }
}
registerProcessor('voice-pcm', VoicePcm);
