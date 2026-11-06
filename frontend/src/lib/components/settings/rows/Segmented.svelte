<script lang="ts" generics="T extends string">
	/**
	 * Segmented: a short set of exclusive choices as pressed buttons
	 * ("Light | Dark | System"). Options are strings or { value, label }.
	 * An `onchange` that reports a failed save (see `settle`) restores the
	 * previous choice.
	 */
	import type { SettingsIcon } from '$lib/settings/categories';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { getRowContext, settle, type ChangeResult } from './row-context';

	type Option = T | { value: T; label: string; icon?: SettingsIcon; disabled?: boolean };

	interface Props {
		options: readonly Option[];
		value: T;
		onchange?: (value: T) => ChangeResult;
		size?: 'md' | 'sm';
		disabled?: boolean;
		/** Group name when not inside a SettingRow. */
		label?: string;
		/** Dialog source selectors keep their radio semantics; Settings uses pressed buttons. */
		selection?: 'pressed' | 'radio';
		stretch?: boolean;
	}

	let { options, value = $bindable(), onchange, size = 'md', disabled = false, label, selection = 'pressed', stretch = false }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const off = $derived(disabled || access.readOnly);
	const items = $derived(
		options.map((o) => (typeof o === 'string' ? { value: o as T, label: o as string, icon: undefined, disabled: false } : { disabled: false, ...o }))
	);

	function pick(next: T) {
		if (off || next === value) return;
		const before = value;
		value = next;
		settle(onchange?.(next), () => (value = before));
	}
</script>

<div
	class="st-seg"
	class:sm={size === 'sm'}
	class:stretch
	role={selection === 'radio' ? 'radiogroup' : 'group'}
	aria-label={label}
	aria-labelledby={label ? undefined : row?.labelId}
>
	{#each items as item (item.value)}
		<button
			type="button"
			role={selection === 'radio' ? 'radio' : undefined}
			aria-pressed={selection === 'pressed' ? item.value === value : undefined}
			aria-checked={selection === 'radio' ? item.value === value : undefined}
			disabled={off || item.disabled}
			onclick={() => pick(item.value)}>{#if item.icon}<item.icon size={14} strokeWidth={1.8} aria-hidden="true" />{/if}{item.label}</button
		>
	{/each}
</div>

<style>
	.st-seg {
		display: inline-flex;
		background: var(--st-muted, var(--muted));
		border: 1px solid var(--st-border, var(--border));
		border-radius: var(--st-radius-md, var(--radius-control));
		padding: 2px;
		gap: 2px;
		max-width: 100%;
		/* A pane narrower than every choice in a row wraps the last ones onto
		   a second line. Scrolling hid them and pushed the pane sideways (#2118). */
		flex-wrap: wrap;
	}
	.st-seg.stretch { display: flex; width: 100%; }
	.st-seg.stretch button { flex: 1; }
	.st-seg button {
		display: inline-flex; align-items: center; justify-content: center; gap: 6px;
		border: 0;
		background: none;
		border-radius: var(--st-radius-sm, var(--radius-control));
		height: 26px;
		padding: 0 11px;
		font-size: 13px;
		color: var(--st-mfg, var(--muted-foreground));
		white-space: nowrap;
	}
	.st-seg button:hover:not(:disabled):not([aria-pressed='true'], [aria-checked='true']) {
		color: var(--st-fg, var(--foreground));
	}
	.st-seg button:is([aria-pressed='true'], [aria-checked='true']) {
		background: var(--segment-selected);
		color: var(--st-fg, var(--foreground));
		box-shadow: 0 0 0 1px var(--st-border-strong, var(--border));
	}
	.st-seg button:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.st-seg button:active:not(:disabled) { transform: translateY(1px); }
	@media (max-width: 600px) { .st-seg { width: 100%; } .st-seg button { flex: 1; min-height: 44px; } }
	.st-seg button:disabled {
		opacity: 0.55;
	}
	.st-seg.sm button {
		height: 22px;
		padding: 0 8px;
		font-size: 12px;
	}
</style>
