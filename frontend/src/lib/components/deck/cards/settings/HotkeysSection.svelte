<script lang="ts">
	import { Mic } from 'lucide-svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import HotkeyCapture from './HotkeyCapture.svelte';
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><Mic />Voice &amp; hotkeys</h3>
		<p>Dictation and the keyboard shortcut that triggers it.</p>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Global voice input</h4><p>Press the hotkey anywhere to start voice-to-text.</p></div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={$hotkeyPreferences.globalSttEnabled}
				role="switch"
				aria-checked={$hotkeyPreferences.globalSttEnabled}
				aria-label="Global voice input"
				onclick={() => hotkeyPreferences.setGlobalSttEnabled(!$hotkeyPreferences.globalSttEnabled)}
			><i></i></button>
		</div>
	</div>
	{#if $hotkeyPreferences.globalSttEnabled}
		<div class="set-row">
			<div class="lab"><h4>Voice input hotkey</h4><p>Click to capture a new key combination.</p></div>
			<div class="ctl">
				<HotkeyCapture
					value={$hotkeyPreferences.globalSttHotkey}
					onChange={(v) => hotkeyPreferences.setGlobalSttHotkey(v)}
				/>
			</div>
		</div>
	{/if}
	<div class="set-row">
		<div class="lab"><h4>Sound cues</h4><p>A short tone when the microphone goes live, stops, or fails — so you can start talking without watching the screen.</p></div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={$hotkeyPreferences.voiceSoundCues}
				role="switch"
				aria-checked={$hotkeyPreferences.voiceSoundCues}
				aria-label="Voice input sound cues"
				onclick={() => hotkeyPreferences.setVoiceSoundCues(!$hotkeyPreferences.voiceSoundCues)}
			><i></i></button>
		</div>
	</div>
	<div class="set-row">
		<div class="lab">
			<p class="hint-row">When triggered, transcribed text is copied to the clipboard. If a text field is focused, it is also pasted automatically.</p>
		</div>
	</div>
</div>

<style>
	.hint-row {
		margin: 0;
	}
</style>
