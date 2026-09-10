<script lang="ts">
	import { Mic } from 'lucide-svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import HotkeyCapture from './HotkeyCapture.svelte';
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><Mic />Voice &amp; hotkeys</h3>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Global voice input</h4></div>
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
			<div class="lab"><h4>Voice input hotkey</h4></div>
			<div class="ctl">
				<HotkeyCapture
					value={$hotkeyPreferences.globalSttHotkey}
					onChange={(v) => hotkeyPreferences.setGlobalSttHotkey(v)}
				/>
			</div>
		</div>
	{/if}
	<div class="set-row">
		<div class="lab"><h4>Sound cues</h4></div>
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
</div>
