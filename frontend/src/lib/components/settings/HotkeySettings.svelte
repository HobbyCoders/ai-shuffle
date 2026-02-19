<script lang="ts">
	/**
	 * HotkeySettings - Global voice input toggle and hotkey shortcut capture
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import HotkeyCapture from './HotkeyCapture.svelte';

	let expanded = $state(false);
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
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

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-row.vertical {
		flex-direction: column;
		align-items: stretch;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.setting-info label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.setting-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.setting-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.5;
		padding: 4px 0;
	}

	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover { border-color: var(--ring); }
	.toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
		box-shadow: var(--shadow-s);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}
</style>
