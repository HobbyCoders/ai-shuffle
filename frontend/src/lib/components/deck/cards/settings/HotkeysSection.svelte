<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import HotkeyCapture from '$lib/components/settings/HotkeyCapture.svelte';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
	}

	let {
		expanded,
		onToggle,
	}: Props = $props();
</script>

<section class="section">
	<button class="section-header" onclick={onToggle}>
		<h3>Hotkeys</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Global Voice Input -->
			<div class="setting-row">
				<div class="setting-info">
					<span>Global Voice Input</span>
					<span class="setting-desc">Press hotkey anywhere to start voice-to-text</span>
				</div>
				<button
				role="switch"
				aria-checked={$hotkeyPreferences.globalSttEnabled}
				onclick={() => hotkeyPreferences.setGlobalSttEnabled(!$hotkeyPreferences.globalSttEnabled)}
				class="toggle"
				class:active={$hotkeyPreferences.globalSttEnabled}
				aria-label="Global Voice Input"
			>
				<span class="toggle-knob"></span>
			</button>
			</div>

			{#if $hotkeyPreferences.globalSttEnabled}
				<div class="setting-row vertical">
					<div class="setting-info">
						<span>Voice Input Hotkey</span>
						<span class="setting-desc">Click to capture a new key combination</span>
					</div>
					<HotkeyCapture
						value={$hotkeyPreferences.globalSttHotkey}
						onChange={(v) => hotkeyPreferences.setGlobalSttHotkey(v)}
					/>
				</div>
			{/if}

			<div class="setting-hint">
				When triggered, transcribed text is copied to clipboard. If a text field is focused, it will also be pasted automatically.
			</div>
		</div>
	{/if}
</section>
