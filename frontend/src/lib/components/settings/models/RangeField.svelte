<script lang="ts">
	/**
	 * RangeField: a slider with its value beside it ("1.00x"). Labelled by the
	 * SettingRow it sits in. Local to Default models until the row set has a
	 * slider primitive.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext } from '../rows/row-context';

	interface Props {
		value: number;
		min: number;
		max: number;
		step?: number;
		/** Text shown beside the slider; defaults to the number. */
		format?: (value: number) => string;
		oninput?: (value: number) => void;
		disabled?: boolean;
		label?: string;
	}

	let { value, min, max, step = 1, format = String, oninput, disabled = false, label }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
</script>

<span class="st-range">
	<input
		type="range"
		{min}
		{max}
		{step}
		{value}
		disabled={disabled || access.readOnly}
		{...controlAria(row, label)}
		oninput={(e) => oninput?.(Number(e.currentTarget.value))}
	/>
	<output class="st-mono">{format(value)}</output>
</span>

<style>
	.st-range {
		display: inline-flex;
		align-items: center;
		gap: 10px;
	}
	input {
		width: 132px;
		accent-color: var(--st-primary);
		margin: 0;
	}
	input:disabled {
		opacity: 0.55;
	}
	output {
		min-width: 44px;
		text-align: right;
		color: var(--st-fg);
		font-variant-numeric: tabular-nums;
	}
</style>
