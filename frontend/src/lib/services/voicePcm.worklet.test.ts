import { describe, expect, it, vi } from 'vitest';
import source from './voicePcm.worklet.js?raw';

describe('microphone worklet mute boundary', () => {
 it('discards partial speech and emits only fresh samples after unmute', () => {
  class Processor { port: any = { onmessage: null, postMessage: vi.fn() }; }
  let Constructor: any;
  // Execute the shipped processor, not a duplicate implementation.
  new Function('AudioWorkletProcessor', 'sampleRate', 'registerProcessor', source)(Processor, 24000, (_name: string, cls: any) => { Constructor = cls; });
  const worklet = new Constructor();
  worklet.process([[new Float32Array(960).fill(0.8)]]);
  worklet.port.onmessage({ data: { type: 'mute', muted: true, generation: 1 } });
  worklet.process([[new Float32Array(1920).fill(0.9)]]);
  expect(worklet.port.postMessage).not.toHaveBeenCalled();
  worklet.port.onmessage({ data: { type: 'mute', muted: false, generation: 2 } });
  worklet.process([[new Float32Array(1920)]]);
  const frame = worklet.port.postMessage.mock.calls[0][0];
  expect(frame.generation).toBe(2);
  expect([...new Int16Array(frame.pcm)].every(value => value === 0)).toBe(true);
 });
 it('ticks about once a second while muted, without audio', () => {
  class Processor { port: any = { onmessage: null, postMessage: vi.fn() }; }
  let Constructor: any;
  new Function('AudioWorkletProcessor', 'sampleRate', 'registerProcessor', source)(Processor, 48000, (_name: string, cls: any) => { Constructor = cls; });
  const worklet = new Constructor();
  worklet.port.onmessage({ data: { type: 'mute', muted: true, generation: 1 } });
  for (let i = 0; i < 374; i++) worklet.process([[new Float32Array(128).fill(0.5)]]);
  expect(worklet.port.postMessage).not.toHaveBeenCalled();
  worklet.process([[new Float32Array(128)]]); // 375 × 128 = 48000 samples: one second.
  expect(worklet.port.postMessage).toHaveBeenCalledExactlyOnceWith({ tick: true });
  // A disabled track may deliver no input at all; the clock still runs.
  for (let i = 0; i < 375; i++) worklet.process([[]]);
  expect(worklet.port.postMessage).toHaveBeenCalledTimes(2);
  worklet.port.onmessage({ data: { type: 'mute', muted: false, generation: 2 } });
  worklet.process([[new Float32Array(48000)]]);
  expect(worklet.port.postMessage.mock.calls.slice(2).every(([m]: any[]) => m.pcm && m.generation === 2)).toBe(true);
 });
});
