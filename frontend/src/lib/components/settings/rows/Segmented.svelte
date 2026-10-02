<script lang="ts" generics="T extends string">
	/**
	 * Segmented: a short set of exclusive choices as pressed buttons
	 * ("Light | Dark | System"). Options are strings or { value, label }.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { getRowContext } from './row-context';

	type Option = T | { value: T; label: string; disabled?: boolean };

	interface Props {
		options: readonly Option[];
		value: T;
		onchange?: (value: T) => void;
		size?: 'md' | 'sm';
		disabled?: boolean;
		/** Group name when not inside a SettingRow. */
		label?: string;
	}

	let { options, value = $bindable(), onchange, size = 'md', disabled = false, label }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const off = $derived(disabled || access.readOnly);
	const items = $derived(
		options.map((o) => (typeof o === 'string' ? { value: o as T, label: o as string, disabled: false } : { disabled: false, ...o }))
	);

	function pick(next: T) {
		if (off || next === value) return;
		value = next;
		onchange?.(next);
	}
</script>

<div
	class="st-seg"
	class:sm={size === 'sm'}
	role="group"
	aria-label={label}
	aria-labelledby={label ? undefined : row?.labelId}
>
	{#each items as item (item.value)}
		<button
			type="button"
			aria-pressed={item.value === value}
			disabled={off || item.disabled}
			onclick={() => pick(item.value)}>{item.label}</button
		>
	{/each}
</div>

<style>
	.st-seg {
		display: inline-flex;
		background: var(--st-muted);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius-md);
		padding: 2px;
		gap: 2px;
		max-width: 100%;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.st-seg button {
		border: 0;
		background: none;
		border-radius: var(--st-radius-sm);
		height: 26px;
		padding: 0 11px;
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.st-seg button:hover:not(:disabled):not([aria-pressed='true']) {
		color: var(--st-fg);
	}
	.st-seg button[aria-pressed='true'] {
		background: var(--st-card);
		color: var(--st-fg);
		box-shadow: 0 0 0 1px var(--st-border-strong);
	}
	.st-seg button:disabled {
		opacity: 0.55;
	}
	.st-seg.sm button {
		height: 22px;
		padding: 0 8px;
		font-size: 12px;
	}
</style>
