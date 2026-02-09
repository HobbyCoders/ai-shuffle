<script lang="ts">
	/**
	 * HotkeyCapture - Click-to-capture keyboard shortcut input
	 *
	 * Usage:
	 * <HotkeyCapture
	 *   value={hotkeyConfig}
	 *   onChange={(config) => updateHotkey(config)}
	 * />
	 */
	import { formatHotkeyConfig } from '$lib/services/keyboard';
	import type { HotkeyConfig } from '$lib/stores/hotkeyPreferences';

	interface Props {
		value: HotkeyConfig;
		onChange: (config: HotkeyConfig) => void;
		disabled?: boolean;
	}

	let { value, onChange, disabled = false }: Props = $props();

	let isCapturing = $state(false);
	let buttonRef: HTMLButtonElement | null = $state(null);

	// Modifier-only keys that should be ignored when pressed alone
	const MODIFIER_KEYS = ['Control', 'Shift', 'Alt', 'Meta'];

	function handleKeyDown(e: KeyboardEvent) {
		if (!isCapturing || disabled) return;

		e.preventDefault();
		e.stopPropagation();

		// Escape cancels capture mode
		if (e.key === 'Escape') {
			isCapturing = false;
			return;
		}

		// Ignore modifier-only presses - wait for actual key
		if (MODIFIER_KEYS.includes(e.key)) {
			return;
		}

		// Build the new hotkey config
		const newConfig: HotkeyConfig = {
			key: e.key.toLowerCase(),
			cmdOrCtrl: e.metaKey || e.ctrlKey,
			shift: e.shiftKey,
			alt: e.altKey
		};

		// Require at least one modifier for safety (avoid capturing single letters)
		if (!newConfig.cmdOrCtrl && !newConfig.shift && !newConfig.alt) {
			// Allow function keys and special keys without modifiers
			const allowedWithoutModifier = [
				'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
				'Insert', 'Delete', 'Home', 'End', 'PageUp', 'PageDown'
			];
			if (!allowedWithoutModifier.includes(e.key)) {
				// Don't accept single character keys without modifiers
				return;
			}
		}

		onChange(newConfig);
		isCapturing = false;
	}

	function handleClick() {
		if (disabled) return;
		isCapturing = true;
		// Focus the button to capture keyboard events
		buttonRef?.focus();
	}

	function handleBlur() {
		// Small delay to allow click events to process
		setTimeout(() => {
			isCapturing = false;
		}, 100);
	}

	// Format the current hotkey for display
	const displayValue = $derived(formatHotkeyConfig(value));
</script>

<button
	bind:this={buttonRef}
	type="button"
	class="hotkey-capture-btn"
	class:capturing={isCapturing}
	class:disabled={disabled}
	onclick={handleClick}
	onkeydown={handleKeyDown}
	onblur={handleBlur}
	disabled={disabled}
	aria-label={isCapturing ? 'Press a key combination' : `Current hotkey: ${displayValue}. Click to change.`}
>
	{#if isCapturing}
		<span class="capture-prompt">
			<svg class="animate-pulse" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<circle cx="12" cy="12" r="10" />
				<circle cx="12" cy="12" r="3" />
			</svg>
			Press a key combination...
		</span>
	{:else}
		<kbd class="hotkey-display">{displayValue}</kbd>
		<span class="edit-hint">Click to change</span>
	{/if}
</button>

<style>
	.hotkey-capture-btn {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		width: 100%;
		padding: 12px 16px;
		background: var(--input);
		border: 2px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
		transition: all 0.2s ease;
		font-family: inherit;
		font-size: 14px;
	}

	.hotkey-capture-btn:hover:not(.disabled) {
		border-color: var(--ring);
		background: var(--hover-overlay);
	}

	.hotkey-capture-btn:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 3px var(--ring-offset);
	}

	.hotkey-capture-btn.capturing {
		border-color: var(--primary);
		background: var(--primary-15);
		animation: pulse-border 1.5s ease-in-out infinite;
	}

	.hotkey-capture-btn.disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.capture-prompt {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--primary);
		font-weight: 500;
	}

	.capture-prompt svg {
		flex-shrink: 0;
	}

	.hotkey-display {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 6px 12px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 6px;
		font-family: var(--font-mono);
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
	}

	.edit-hint {
		color: var(--muted-foreground);
		font-size: 12px;
	}

	@keyframes pulse-border {
		0%, 100% {
			border-color: var(--primary);
			box-shadow: 0 0 0 0 var(--primary-25);
		}
		50% {
			border-color: var(--primary-75);
			box-shadow: 0 0 0 4px var(--primary-15);
		}
	}
</style>
