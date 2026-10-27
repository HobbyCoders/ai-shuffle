import { describe, expect, it, vi } from 'vitest';
import source from './voicePlayback.worklet.js?raw';

/** The shipped processor, run with a fake audio thread at `rate` Hz. */
function player(rate = 48000) {
 class Processor { port: any = { onmessage: null, postMessage: vi.fn() }; }
 let Constructor: any;
 new Function('AudioWorkletProcessor', 'sampleRate', 'registerProcessor', source)(Processor, rate, (_name: string, cls: any) => { Constructor = cls; });
 const node = new Constructor();
 const push = (pcm: Int16Array) => node.port.onmessage({ data: { type: 'pcm', pcm: pcm.buffer } });
 /** Render `ms` of output; returns the samples. */
 const render = (ms: number) => {
  const out: number[] = [];
  for (let left = Math.round(rate * ms / 1000); left > 0; left -= 128) {
   const block = new Float32Array(Math.min(128, left));
   node.process([], [[block]]);
   out.push(...block);
  }
  return out;
 };
 const messages = (type: string) => node.port.postMessage.mock.calls.map(([m]: any[]) => m).filter((m: any) => m.type === type);
 return { node, push, render, messages };
}
const frame = (value: number, ms = 100) => new Int16Array(24 * ms).fill(value);
/** `count` frames of `ms` each carrying one continuous ramp (never 0), and the ramp itself. */
function ramp(count: number, ms: number) {
 const all = Int16Array.from({ length: count * 24 * ms }, (_, n) => 1 + (n % 32000));
 return { all, frames: Array.from({ length: count }, (_, k) => all.subarray(k * 24 * ms, (k + 1) * 24 * ms).slice()) };
}
/**
 * Deliver `frames` at `arrivals` (ms) while the device plays in 10 ms steps.
 * Returns every output sample and, per 10 ms step, whether it played speech
 * and whether the player said it was speaking at the end of that step.
 */
function deliver(p: ReturnType<typeof player>, frames: Int16Array[], arrivals: number[], totalMs: number) {
 const out: number[] = [], steps: { sound: boolean; speaking: boolean }[] = [];
 let k = 0;
 for (let t = 0; t < totalMs; t += 10) {
  while (k < frames.length && arrivals[k] <= t) p.push(frames[k++]);
  const chunk = p.render(10);
  for (const v of chunk) out.push(v);
  steps.push({ sound: chunk.some(v => v !== 0), speaking: !!p.messages('speaking').at(-1)?.active });
 }
 return { out, steps };
}

describe('voice playback worklet', () => {
 it('drops queued speech on provider barge-in and accepts the next reply', () => {
  const p = player();
  p.push(frame(8000, 500));
  p.render(100);
  p.node.port.onmessage({ data: { type: 'clear' } });
  expect(p.render(100).every(v => v === 0)).toBe(true);
  expect(p.messages('speaking').at(-1)).toEqual({ type: 'speaking', active: false });
  p.push(frame(4000, 100));
  expect(p.render(500).some(v => v !== 0)).toBe(true);
 });
 it('plays the end of a reply to its last sample, even below the jitter target', () => {
  const p = player();
  p.push(frame(8000, 60)); // the short tail of a reply
  const out = p.render(1000);
  expect(out.filter(v => v !== 0)).toHaveLength(60 * 48);
  expect(p.messages('speaking')).toEqual([{ type: 'speaking', active: true }, { type: 'speaking', active: false }]);
 });

 it.each([48000, 44100, 16000])('plays 24 kHz speech at the right speed on a %i Hz device', rate => {
  const p = player(rate);
  p.push(frame(8000, 500));
  const played = p.render(1500).filter(v => v !== 0).length;
  expect(Math.abs(played - rate / 2)).toBeLessThanOrEqual(2);
 });

 it('rides out late frames with its buffer instead of cutting in and out', () => {
  const p = player();
  // Real-time speech whose 100 ms frames arrive up to 40 ms late.
  const jitter = [0, 40, 10, 40, 0, 30, 40, 0, 20, 40];
  const arrivals = Array.from({ length: 30 }, (_, k) => 100 * k + jitter[k % jitter.length]);
  const out: number[] = [];
  for (let t = 0; t < 3600; t += 10) {
   while (arrivals.length && arrivals[0] <= t) { arrivals.shift(); p.push(frame(8000)); }
   out.push(...p.render(10));
  }
  const first = out.findIndex(v => v !== 0), last = out.findLastIndex(v => v !== 0);
  expect(last - first + 1).toBe(30 * 100 * 48); // three seconds, without a gap
  expect(out.slice(first, last + 1).every(v => v !== 0)).toBe(true);
  expect(p.messages('stats').at(-1)).toMatchObject({ underruns: 0 });
 });

 it('counts an underrun and holds more audio afterwards', () => {
  const p = player();
  p.push(frame(8000, 200));
  p.render(300); // ran dry
  p.push(frame(8000)); // speech resumes: that gap was an underrun
  p.render(1000);
  expect(p.messages('stats').at(-1)).toMatchObject({ underruns: 1, targetMs: 200 });
 });

 it('cuts an oversized backlog once, oldest first, and plays the rest in order', () => {
  const p = player();
  // A 10 s burst after a main-thread stall; frame i carries the value i.
  for (let i = 1; i <= 100; i++) p.push(frame(i * 100));
  const values = p.render(6000).map(v => Math.round(v * 32768)).filter(v => v > 0 && v % 100 === 0);
  const frames = [...new Set(values)].map(v => v / 100);
  // One contiguous run ending with the newest speech: nothing thinned out.
  expect(frames).toEqual(Array.from({ length: frames.length }, (_, i) => frames[0] + i));
  expect(frames.at(-1)).toBe(100);
  expect(frames.length).toBeGreaterThanOrEqual(45);
  expect(p.messages('stats').at(-1).droppedMs).toBeGreaterThan(0);
 });

 // A provider that sends speech faster than real time (#731): a 27 s reply in
 // 5 s, Grok's observed 37 frames of ~0.73 s over 19 s, and sentences sent in
 // quick clumps with pauses between them.
 const bursts = {
  '27 s in 5 s': { count: 270, ms: 100, at: (k: number) => k * 5000 / 270 },
  'Grok pacing': { count: 37, ms: 730, at: (k: number) => k * 19000 / 37 },
  'sentence clumps': { count: 300, ms: 100, at: (k: number) => Math.floor(k / 60) * 2500 + (k % 60) * 400 / 60 },
 };
 it.each(Object.entries(bursts).flatMap(([name, b]) => [24000, 48000].map(rate => [name, rate, b] as const)))(
  'plays a burst (%s) in full and in order on a %i Hz device', (_name, rate, b) => {
   const p = player(rate);
   const { all, frames } = ramp(b.count, b.ms);
   const seconds = all.length / 24000;
   const { out, steps } = deliver(p, frames, frames.map((_, k) => b.at(k)), (seconds + 2) * 1000);
   const first = out.findIndex(v => v !== 0), last = out.findLastIndex(v => v !== 0);
   const run = out.slice(first, last + 1);
   // One run with no gap, no speed-up and nothing dropped.
   expect(run.length).toBe(all.length * rate / 24000);
   expect(run.every(v => v !== 0)).toBe(true);
   // Every sample, in order (at 48 kHz the in-between outputs are interpolated).
   const played = run.filter((_, i) => i % (rate / 24000) === 0).map(v => Math.round(v * 32768));
   expect(played.findIndex((v, i) => v !== all[i])).toBe(-1);
   expect(p.node.droppedMs).toBe(0);
   expect(p.messages('stats').every((m: any) => m.droppedMs === 0)).toBe(true);
   expect(Math.max(...p.messages('stats').map((m: any) => m.maxLeadMs))).toBeGreaterThan(1000);
   // Speaking from the first sample to the last, then off once.
   expect(steps.filter(s => s.sound).every(s => s.speaking)).toBe(true);
   expect(p.messages('speaking')).toEqual([{ type: 'speaking', active: true }, { type: 'speaking', active: false }]);
  });

 it('still cuts the late clump released after a stall in the middle of a reply', () => {
  const p = player();
  for (let i = 1; i <= 20; i++) { p.push(frame(i * 100)); p.render(100); } // 2 s in real time
  p.render(8000); // the main thread stalls for 8 s; the player runs dry
  for (let i = 21; i <= 120; i++) p.push(frame(i * 100)); // 10 s of speech released at once
  const values = p.render(6000).map(v => Math.round(v * 32768)).filter(v => v > 0 && v % 100 === 0);
  const frames = [...new Set(values)].map(v => v / 100);
  expect(frames).toEqual(Array.from({ length: frames.length }, (_, i) => frames[0] + i));
  expect(frames[0]).toBeGreaterThan(21);
  expect(frames.at(-1)).toBe(120);
  expect(p.node.droppedMs).toBeGreaterThan(5000);
 });

 it('caps a runaway stream at its ring instead of growing without bound', () => {
  const p = player(24000);
  // 120 s of speech at eight times real time.
  for (let t = 0; t < 15000; t += 100) { p.push(frame(1000, 800)); p.render(100); }
  const stats = p.messages('stats');
  expect(Math.max(...stats.map((m: any) => m.maxDepthMs))).toBeLessThanOrEqual(88000);
  expect(p.node.droppedMs).toBeGreaterThan(0);
  expect(p.render(1000).every(v => v !== 0)).toBe(true); // still playing what it kept
 });

 it('empties a long buffered burst at once on barge-in', () => {
  const p = player();
  const { frames } = ramp(270, 100);
  deliver(p, frames, frames.map((_, k) => k * 5000 / 270), 5000);
  expect(p.node.depth()).toBeGreaterThan(20 * 24000);
  p.node.port.onmessage({ data: { type: 'clear' } });
  expect(p.node.depth()).toBe(0);
  expect(p.render(500).every(v => v === 0)).toBe(true);
  expect(p.messages('speaking').at(-1)).toEqual({ type: 'speaking', active: false });
  expect(p.node.droppedMs).toBe(0);
 });
});
