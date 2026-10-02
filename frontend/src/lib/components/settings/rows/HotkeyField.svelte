<script lang="ts">
	/**
	 * HotkeyField: the shortcut as key caps. Click, then press the new
	 * combination; Esc cancels. Letters need Ctrl/Cmd, Alt or Shift; function
	 * and navigation keys work alone.
	 */
	import { tick } from 'svelte';
	import { formatHotkeyConfig } from '$lib/services/keyboard';
	import type { HotkeyConfig } from '$lib/stores/hotkeyPreferences.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { getRowContext } from './row-context';

	interface Props {
		value: HotkeyConfig;
		onchange: (config: HotkeyConfig) => void;
		disabled?: boolean;
		/** Accessible name when not inside a SettingRow. */
		label?: string;
	}

	let { value, onchange, disabled = false, label }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const off = $derived(disabled || access.readOnly);
	const name = $derived(label ?? row?.label ?? 'Hotkey');
	const keys = $derived(formatHotkeyConfig(value).split('+'));

	const MODIFIERS = ['Control', 'Shift', 'Alt', 'Meta'];
	const ALONE = /^(F\d{1,2}|Insert|Delete|Home|End|PageUp|PageDown)$/;

	let capturing = $state(false);
	let needsModifier = $state(false);
	let button: HTMLButtonElement | undefined = $state();

	async function start() {
		if (off) return;
		capturing = true;
		needsModifier = false;
		await tick();
		button?.focus();
	}

	function onkeydown(e: KeyboardEvent) {
		if (!capturing) return;
		e.preventDefault();
		e.stopPropagation();
		if (e.key === 'Escape') {
			capturing = false;
			return;
		}
		if (MODIFIERS.includes(e.key)) return;
		const config: HotkeyConfig = {
			key: e.key.toLowerCase(),
			cmdOrCtrl: e.metaKey || e.ctrlKey,
			shift: e.shiftKey,
			alt: e.altKey
		};
		if (!config.cmdOrCtrl && !config.shift && !config.alt && !ALONE.test(e.key)) {
			needsModifier = true;
			return;
		}
		capturing = false;
		onchange(config);
	}
</script>

<button
	bind:this={button}
	type="button"
	class="st-hotkey"
	class:on={capturing}
	disabled={off}
	aria-label={capturing ? `${name}: press keys, Esc cancels` : `${name}: ${keys.join(' ')}. Change`}
	aria-describedby={row?.hintId}
	onclick={start}
	{onkeydown}
	onblur={() => (capturing = false)}
>
	{#if capturing}
		{needsModifier ? 'Add Ctrl, Alt or Cmd' : 'Press keys, Esc cancels'}
	{:else}
		<kbd>{#each keys as key, i (i)}<span>{key}</span>{/each}</kbd>
	{/if}
</button>

<style>
	.st-hotkey {
		background: none;
		border: 1px dashed transparent;
		border-radius: var(--st-radius-md);
		padding: 3px 4px;
		color: var(--st-fg);
		font: inherit;
	}
	.st-hotkey:hover:not(:disabled) {
		border-color: var(--st-border-strong);
	}
	.st-hotkey:disabled {
		opacity: 0.55;
	}
	.st-hotkey.on {
		border-color: var(--st-primary);
		color: var(--st-primary);
		font-size: 12.5px;
		padding: 5px 10px;
		white-space: nowrap;
	}
	kbd {
		display: inline-flex;
		gap: 3px;
		font-family: var(--st-mono);
	}
	kbd span {
		border: 1px solid var(--st-border-strong);
		border-bottom-width: 2px;
		border-radius: 5px;
		padding: 1px 6px;
		font-size: 11.5px;
		background: var(--st-muted);
	}
</style>
