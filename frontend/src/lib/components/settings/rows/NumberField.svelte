<script lang="ts">
	/**
	 * NumberField: a number with its unit. Commits on blur or Enter, clamped to
	 * [min, max]; an empty or invalid entry reverts to the current value.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext } from './row-context';

	interface Props {
		value: number;
		onchange?: (value: number) => void;
		min?: number;
		max?: number;
		step?: number;
		unit?: string;
		disabled?: boolean;
		label?: string;
	}

	let { value = $bindable(0), onchange, min, max, step = 1, unit, disabled = false, label }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	let draft = $state('');
	let editing = $state(false);
	const shown = $derived(editing ? draft : String(value));

	function commit() {
		editing = false;
		const n = Number(draft);
		if (draft.trim() === '' || !Number.isFinite(n)) return;
		let next = n;
		if (min !== undefined) next = Math.max(min, next);
		if (max !== undefined) next = Math.min(max, next);
		if (next === value) return;
		value = next;
		onchange?.(next);
	}
</script>

<label class="st-num">
	<input
		type="number"
		{min}
		{max}
		{step}
		value={shown}
		disabled={disabled || access.readOnly}
		{...controlAria(row, label)}
		oninput={(e) => {
			editing = true;
			draft = e.currentTarget.value;
		}}
		onblur={commit}
		onkeydown={(e) => {
			if (e.key === 'Enter') {
				e.preventDefault();
				commit();
			} else if (e.key === 'Escape' && editing) {
				e.stopPropagation();
				editing = false;
			}
		}}
	/>
	{#if unit}<span>{unit}</span>{/if}
</label>

<style>
	.st-num {
		display: inline-flex;
		align-items: center;
		height: 30px;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		overflow: hidden;
	}
	.st-num input {
		width: 56px;
		border: 0;
		background: none;
		text-align: right;
		padding: 0 4px 0 8px;
		font-variant-numeric: tabular-nums;
		font-size: 13px;
		color: var(--st-fg);
		-moz-appearance: textfield;
		appearance: textfield;
	}
	.st-num input::-webkit-inner-spin-button,
	.st-num input::-webkit-outer-spin-button {
		-webkit-appearance: none;
	}
	.st-num input:focus {
		outline: none;
	}
	.st-num input:disabled {
		opacity: 0.55;
	}
	.st-num span {
		font-size: 12.5px;
		color: var(--st-mfg);
		padding-right: 10px;
		white-space: nowrap;
	}
	.st-num:not(:has(span)) input {
		padding-right: 8px;
	}
	.st-num:focus-within {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
</style>
