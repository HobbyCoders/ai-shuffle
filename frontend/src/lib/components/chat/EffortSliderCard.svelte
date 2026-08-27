<script lang="ts">
	/**
	 * EffortSliderCard — the reasoning-effort stepper card shared by the mobile
	 * controls sheet and the desktop effort popover: current level + Fast-tier
	 * toggle on top, a stepped range across the levels, labels underneath.
	 */
	import { Zap } from 'lucide-svelte';

	interface ControlOption {
		value: string;
		label: string;
		description: string;
	}

	interface Props {
		options: ControlOption[];
		value: string | null;
		onSelect: (value: string) => void;
		/** Whether the picked model carries the fast service tier. */
		fastSupported?: boolean;
		fastEnabled?: boolean;
		onToggleFast?: () => void;
		/** Drop the card's own surface (background/border/shadow) when a parent
		 *  frame already provides one — the desktop popover. */
		frameless?: boolean;
	}

	let {
		options,
		value,
		onSelect,
		fastSupported = false,
		fastEnabled = false,
		onToggleFast,
		frameless = false
	}: Props = $props();

	const index = $derived.by(() => {
		const i = options.findIndex((option) => option.value === value);
		return i < 0 ? 0 : i;
	});
	const selected = $derived(options[index] ?? options[0]);
	const progress = $derived(options.length > 1 ? (index / (options.length - 1)) * 100 : 0);

	function handleInput(event: Event) {
		const option = options[Number((event.currentTarget as HTMLInputElement).value)];
		if (option && option.value !== value) onSelect(option.value);
	}
</script>

<div class="effort-slider-card" class:frameless>
	<div class="effort-current">
		<span>
			<strong>{selected?.label ?? 'Effort'}</strong>
			<small>{selected?.description ?? 'Reasoning depth'}</small>
		</span>
		{#if fastSupported}
			<button
				type="button"
				class="fast-toggle"
				class:on={fastEnabled}
				onclick={() => onToggleFast?.()}
				aria-pressed={fastEnabled}
				aria-label="Fast mode"
				title="Fast mode: faster output at a higher price. Switching clears the prompt cache."
			>
				<Zap size={16} strokeWidth={2} aria-hidden="true" />
			</button>
		{/if}
	</div>
	<input
		type="range"
		min="0"
		max={Math.max(options.length - 1, 0)}
		step="1"
		value={index}
		style={`--effort-progress: ${progress}%`}
		aria-label="Reasoning effort"
		aria-valuetext={selected?.label ?? 'Effort'}
		oninput={handleInput}
	/>
	<div
		class="effort-labels"
		style:grid-template-columns={`repeat(${Math.max(options.length, 1)}, 0fr)`}
		aria-hidden="true"
	>
		{#each options as option (option.value)}
			<span class:selected={option.value === value}>{option.label}</span>
		{/each}
	</div>
</div>

<style>
	.effort-slider-card {
		padding: 12px 14px 10px;
		background: color-mix(in oklch, var(--muted) 55%, var(--card));
		border: 1px solid var(--border-subtle);
		border-radius: 16px;
		box-shadow: 0 1px 0 color-mix(in oklch, var(--foreground) 4%, transparent);
	}

	/* Inside a parent popover frame the card loses its own surface so the two
	   borders don't stack. */
	.effort-slider-card.frameless {
		padding: 10px 12px 8px;
		background: transparent;
		border: none;
		border-radius: 0;
		box-shadow: none;
	}

	.effort-current {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 10px;
	}

	.effort-current > span {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.effort-current strong {
		font-size: 0.9rem;
		font-weight: 650;
		color: var(--foreground);
	}

	.effort-current small {
		font-size: 0.72rem;
		color: var(--muted-foreground);
	}

	/* Fast-tier toggle — top-right of the effort card, opposite the current
	   effort copy. Muted until on, then a primary-tinted chip. */
	.fast-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		flex: 0 0 auto;
		align-self: flex-start;
		width: 34px;
		height: 34px;
		background: transparent;
		border: 1px solid var(--border-subtle);
		border-radius: 11px;
		color: var(--subtle-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease, border-color 0.13s ease;
	}

	.fast-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.fast-toggle.on {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		color: var(--primary);
	}

	.fast-toggle.on :global(svg) {
		fill: currentColor;
	}

	.effort-slider-card input[type='range'] {
		width: 100%;
		height: 22px;
		margin: 0;
		appearance: none;
		-webkit-appearance: none;
		background: transparent;
		cursor: pointer;
	}

	.effort-slider-card input[type='range']::-webkit-slider-runnable-track {
		height: 5px;
		border-radius: 999px;
		background: linear-gradient(
			to right,
			var(--primary) 0 var(--effort-progress),
			color-mix(in oklch, var(--muted-foreground) 25%, transparent) var(--effort-progress) 100%
		);
	}

	.effort-slider-card input[type='range']::-moz-range-track {
		height: 5px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted-foreground) 25%, transparent);
	}

	.effort-slider-card input[type='range']::-moz-range-progress {
		height: 5px;
		border-radius: 999px;
		background: var(--primary);
	}

	.effort-slider-card input[type='range']::-webkit-slider-thumb {
		width: 20px;
		height: 20px;
		margin-top: -7.5px;
		appearance: none;
		-webkit-appearance: none;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 5px color-mix(in oklch, #000 24%, transparent);
	}

	.effort-slider-card input[type='range']::-moz-range-thumb {
		width: 14px;
		height: 14px;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 5px color-mix(in oklch, #000 24%, transparent);
	}

	.effort-slider-card input[type='range']:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 999px;
	}

	/* Zero-width tracks spread with space-between put each label's center on
	   its tick position (0%…100%), matching where the range thumb actually
	   stops — the old 1fr columns centered labels at 10%…90% instead. */
	.effort-labels {
		display: grid;
		grid-template-columns: repeat(5, 0fr);
		justify-content: space-between;
		/* The thumb is 20px, so its center at min/max sits 10px in from the
		   edge — inset the label row by the same amount so the end labels
		   center on the end thumbs too. */
		padding: 0 10px;
		margin-top: 2px;
		color: var(--subtle-foreground);
		font-size: 0.62rem;
		font-weight: 550;
	}

	.effort-labels span {
		width: max-content;
		justify-self: center;
		white-space: nowrap;
	}

	.effort-labels span.selected {
		color: var(--primary);
	}
</style>
