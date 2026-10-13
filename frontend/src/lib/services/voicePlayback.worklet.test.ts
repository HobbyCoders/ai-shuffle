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

describe('voice playback worklet', () => {
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
});
