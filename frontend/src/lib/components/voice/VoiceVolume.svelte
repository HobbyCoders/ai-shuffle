<script lang="ts">
 import { voiceVolume, setVoiceVolume, MAX_VOICE_VOLUME } from '$lib/services/voiceVolume';
</script>

<label class="voice-volume">
 <span>Voice volume <span class="value">{$voiceVolume}%</span></span>
 <input type="range" min="0" max={MAX_VOICE_VOLUME} step="10" value={$voiceVolume}
  aria-label="Voice playback volume" aria-valuetext={`${$voiceVolume}%${$voiceVolume > 100 ? ' (boosted)' : ''}`}
  oninput={event => setVoiceVolume(Number(event.currentTarget.value))} />
 <small>{$voiceVolume > 100 ? 'Boosted · lower if distorted' : 'Saved on this device'}</small>
</label>

<style>
 .voice-volume { display: flex; flex-direction: column; gap: 4px; width: 180px; max-width: 100%; font-size: 12px; color: var(--foreground); }
 span { display: flex; justify-content: space-between; gap: 8px; }
 .value { font-variant-numeric: tabular-nums; }
 input { width: 100%; margin: 0; min-height: 32px; accent-color: var(--primary); }
 small { color: var(--muted-foreground); font-size: 11px; }
</style>
