<script lang="ts">
	/**
	 * Switch: the only on/off control in Settings. `role="switch"` with
	 * `aria-checked`. Inside a SettingRow it is labelled by the row; elsewhere
	 * pass `label`. Disabled while Settings is read-only. An `onchange`
	 * that reports a failed save (see `settle`) flips it back.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext, settle, type ChangeResult } from './row-context';

	interface Props {
		checked: boolean;
		onchange?: (checked: boolean) => ChangeResult;
		size?: 'md' | 'sm';
		disabled?: boolean;
		/** Accessible name when not inside a SettingRow (or to override it). */
		label?: string;
	}

	let { checked = $bindable(false), onchange, size = 'md', disabled = false, label }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const off = $derived(disabled || access.readOnly);

	function toggle() {
		if (off) return;
		const before = checked;
		checked = !before;
		settle(onchange?.(checked), () => (checked = before));
	}
</script>

<button
	type="button"
	role="switch"
	class="st-sw"
	class:sm={size === 'sm'}
	aria-checked={checked}
	disabled={off}
	{...controlAria(row, label)}
	onclick={toggle}
></button>

<style>
	.st-sw {
		width: 36px;
		height: 21px;
		border-radius: 999px;
		border: 0;
		padding: 0;
		background: var(--st-border-strong);
		position: relative;
		transition: background 0.15s;
		flex: none;
	}
	.st-sw::after {
		content: '';
		position: absolute;
		top: 2.5px;
		left: 2.5px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--st-knob);
		box-shadow: 0 1px 2px color-mix(in oklch, var(--st-bg) 60%, transparent);
		transition: transform 0.15s;
	}
	.st-sw[aria-checked='true'] {
		background: var(--st-primary);
	}
	/* On the primary track the knob takes primary-foreground: themes with a
	   white primary in dark mode (Vercel) otherwise draw white on white. */
	.st-sw[aria-checked='true']::after {
		transform: translateX(15px);
		background: var(--st-pfg);
	}
	.st-sw:disabled {
		opacity: 0.4;
	}
	.st-sw.sm {
		width: 30px;
		height: 18px;
	}
	.st-sw.sm::after {
		width: 13px;
		height: 13px;
	}
	.st-sw.sm[aria-checked='true']::after {
		transform: translateX(12px);
	}
</style>
