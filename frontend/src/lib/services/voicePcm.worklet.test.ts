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
});
