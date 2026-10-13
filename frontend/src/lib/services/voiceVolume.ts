import { writable, get } from 'svelte/store';

const KEY = 'voice-playback-volume';
export const MAX_VOICE_VOLUME = 300;
export function clampVoiceVolume(value: number): number {
 return Number.isFinite(value) ? Math.max(0, Math.min(MAX_VOICE_VOLUME, value)) : 100;
}
function load(): number {
 try {
  const saved = localStorage.getItem(KEY);
  return saved === null ? 100 : clampVoiceVolume(Number(saved));
 } catch { return 100; }
}
/** Device/origin-local: phone speakers and headphones need different levels. */
export const voiceVolume = writable(load());
export function setVoiceVolume(value: number): void {
 const volume = clampVoiceVolume(value);
 voiceVolume.set(volume);
 try { localStorage.setItem(KEY, String(volume)); } catch { /* private browsing */ }
}

/** Speech gain followed by a peak compressor, shared by PCM and WebRTC playback. */
export function createVoiceOutput(context: AudioContext) {
 const gain = context.createGain();
 const limiter = context.createDynamicsCompressor();
 limiter.threshold.value = -3;
 limiter.knee.value = 0;
 limiter.ratio.value = 20;
 limiter.attack.value = 0.003;
 limiter.release.value = 0.1;
 gain.connect(limiter); limiter.connect(context.destination);
 let muted = false;
 const apply = () => { gain.gain.value = muted ? 0 : get(voiceVolume) / 100; };
 const unsubscribe = voiceVolume.subscribe(apply);
 return {
  input: gain,
  mute(value: boolean) { muted = value; apply(); },
  close() { unsubscribe(); gain.disconnect(); limiter.disconnect(); },
 };
}
