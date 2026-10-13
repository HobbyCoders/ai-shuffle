<script lang="ts">
	/**
	 * Meter: usage against a cap ("7 of 12", or "38% used" with unit '%').
	 * Turns amber at `warnAt` (fraction of the cap).
	 */
	interface Props {
		used: number;
		cap: number;
		/** '%' shows "N% used"; any other unit is appended to the cap. */
		unit?: string;
		warnAt?: number;
		label?: string;
	}

	let { used, cap, unit = '', warnAt = 0.8, label }: Props = $props();

	const ratio = $derived(cap > 0 ? Math.max(0, used / cap) : 0);
	const text = $derived(unit === '%' ? `${used}% used` : `${used} of ${cap}${unit}`);
</script>

<div
	class="st-meter"
	role="meter"
	aria-label={label}
	aria-valuenow={used}
	aria-valuemin={0}
	aria-valuemax={unit === '%' ? 100 : cap}
	aria-valuetext={text}
>
	<div class="lbl" aria-hidden="true">
		{#if unit === '%'}<b>{used}%</b> used{:else}<b>{used}</b> of {cap}{unit}{/if}
	</div>
	<div class="bar"><i class:w={ratio >= warnAt} style:width="{Math.min(100, ratio * 100)}%"></i></div>
</div>

<style>
	.st-meter {
		display: grid;
		gap: 5px;
		width: 190px;
	}
	.bar {
		height: 5px;
		border-radius: 3px;
		background: var(--st-muted);
		overflow: hidden;
	}
	.bar i {
		display: block;
		height: 100%;
		background: var(--st-meter);
		border-radius: 3px;
	}
	.bar i.w {
		background: var(--st-warning);
	}
	.lbl {
		font-size: 12.5px;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
		text-align: right;
	}
	.lbl b {
		color: var(--st-fg);
		font-weight: 500;
	}
</style>
