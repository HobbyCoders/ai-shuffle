import { afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { clampVoiceVolume, createVoiceOutput, setVoiceVolume, voiceVolume } from './voiceVolume';

afterEach(() => { setVoiceVolume(100); localStorage.clear(); vi.restoreAllMocks(); });
it('clamps and persists device-local volume, including boost', async () => {
 setVoiceVolume(250);
 expect(get(voiceVolume)).toBe(250);
 expect(localStorage.getItem('voice-playback-volume')).toBe('250');
 vi.resetModules();
 const reloaded = await import('./voiceVolume');
 expect(get(reloaded.voiceVolume)).toBe(250);
 expect(clampVoiceVolume(-1)).toBe(0);
 expect(clampVoiceVolume(999)).toBe(300);
 expect(clampVoiceVolume(NaN)).toBe(100);
});
it('connects gain through a peak compressor and responds to the store', () => {
 const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
 const limiter = { threshold: {value: 0}, knee: {value: 0}, ratio: {value: 0}, attack: {value: 0}, release: {value: 0}, connect: vi.fn(), disconnect: vi.fn() };
 const context = { createGain: () => gain, createDynamicsCompressor: () => limiter, destination: {} };
 const output = createVoiceOutput(context as unknown as AudioContext);
 expect(gain.connect).toHaveBeenCalledWith(limiter);
 expect(limiter.connect).toHaveBeenCalledWith(context.destination);
 expect(limiter.threshold.value).toBe(-3); expect(limiter.ratio.value).toBe(20);
 setVoiceVolume(200); expect(gain.gain.value).toBe(2);
 output.close(); expect(limiter.disconnect).toHaveBeenCalledOnce();
});
it('still works when storage is unavailable', () => {
 vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
 expect(() => setVoiceVolume(150)).not.toThrow(); expect(get(voiceVolume)).toBe(150);
});
