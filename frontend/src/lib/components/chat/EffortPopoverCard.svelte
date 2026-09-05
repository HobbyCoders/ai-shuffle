<script lang="ts">
	/**
	 * EffortPopoverCard — the compact reasoning-effort card behind the desktop
	 * composer's effort tag (mockup: docs/design/composer-split-chips.mockup.html,
	 * state 03).
	 *
	 * Header "Effort · <level>" with a model-specific slider, tick marks, the stop
	 * names, and Faster / Smarter end labels. The Fast service tier rides along
	 * in the header when the picked model carries it, so the compact card never
	 * hides a knob the full controls popover offers.
	 *
	 * `value === null` means no level is pinned for the chat — the model runs at
	 * its own default. The slider still needs a resting position, so the thumb
	 * sits on the declared default (or first stop) without selecting it; the first
	 * drag pins a level.
	 */
	import { Zap } from 'lucide-svelte';
	import { NO_ADJUSTABLE_EFFORT } from '$lib/constants/model-effort';

	interface ControlOption {
		value: string;
		label: string;
		description: string;
	}

	interface Props {
		options: ControlOption[];
		explanation?: string;
		known?: boolean;
		defaultValue?: string | null;
		value: string | null;
		onSelect: (value: string) => void;
		/** Whether the picked model carries the fast service tier. */
		fastSupported?: boolean;
		fastEnabled?: boolean;
		onToggleFast?: () => void;
	}

	let {
		options,
		explanation = NO_ADJUSTABLE_EFFORT,
		known = true,
		defaultValue = null,
		value,
		onSelect,
		fastSupported = false,
		fastEnabled = false,
		onToggleFast
	}: Props = $props();

	const pinnedIndex = $derived(options.findIndex((option) => option.value === value));
	const pinned = $derived(pinnedIndex >= 0);
	const maxIndex = $derived(Math.max(options.length - 1, 0));
	const index = $derived(pinned ? pinnedIndex : Math.max(0, options.findIndex(option => option.value === defaultValue)));
	const current = $derived(options[index]);
	const currentLabel = $derived(!known ? (value ?? 'Unknown') : !options.length ? 'Not adjustable' : pinned ? (current?.label ?? 'Effort') : 'Auto');
	const progress = $derived(maxIndex > 0 ? (index / maxIndex) * 100 : 0);

	function handleInput(event: Event) {
		const option = options[Number((event.currentTarget as HTMLInputElement).value)];
		if (option && option.value !== value) onSelect(option.value);
	}
</script>

<div class="effort-pop-card">
	<div class="pop-head">
		<span class="pop-title">Effort</span>
		<span class="pop-value-slot">
			{#key currentLabel}
				<span class="pop-value" data-effort-value>{currentLabel}</span>
			{/key}
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
				<Zap size={13} strokeWidth={2} aria-hidden="true" />
			</button>
		{/if}
	</div>

	{#if options.length === 0}
		<p class="effort-explanation">{explanation}</p>
	{:else if options.length === 1}
		<button type="button" class="single-effort" aria-pressed={pinned}
			onclick={() => onSelect(options[0].value)}>{options[0].label}</button>
	{:else}
	<!-- The track, fill and ticks are drawn by hand underneath a native range
	     input whose own track is transparent, so the thumb stays a real,
	     keyboard-reachable control while the marks sit between it and the
	     track. Everything decorative is inset by half the thumb width so the
	     stops line up with where the thumb actually rests. -->
	<div class="slider" class:unpinned={!pinned}>
		<div class="track" aria-hidden="true"></div>
		<div class="fill" style="width: {pinned ? progress : 0}%" aria-hidden="true"></div>
		<div class="ticks" aria-hidden="true">
			{#each options as option, i (option.value)}
				<span class="tick" class:on={pinned && i <= index}></span>
			{/each}
		</div>
		<input
			type="range"
			min="0"
			max={maxIndex}
			step="1"
			value={index}
			aria-label="Reasoning effort"
			aria-valuetext={currentLabel}
			oninput={handleInput}
		/>
	</div>
	<div class="stops" style={`grid-template-columns: repeat(${options.length}, 0fr)`} aria-hidden="true">
		{#each options as option, i (option.value)}
			<span class:on={pinned && i === index}>{option.label}</span>
		{/each}
	</div>
	<div class="ends" aria-hidden="true"><span>Faster</span><span>Smarter</span></div>
	{/if}
</div>

<style>
	.effort-explanation { font-size: 12px; color: var(--muted-foreground); }
	.single-effort { padding: 6px 12px; border-radius: 6px; background: var(--muted); color: var(--foreground); }
	.single-effort:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.effort-pop-card {
		padding: 12px 14px;
		min-width: 0;
	}

	.pop-head {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 14px;
	}

	.pop-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
	}

	.pop-value-slot {
		position: relative;
		display: inline-grid;
		align-items: center;
		overflow: hidden;
	}

	.pop-value {
		grid-area: 1 / 1;
		font-size: 13px;
		color: var(--muted-foreground);
		animation: pop-value-in 200ms ease-out;
	}

	@keyframes pop-value-in {
		from {
			opacity: 0;
			transform: translateY(0.45em);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.fast-toggle {
		display: grid;
		place-items: center;
		width: 20px;
		height: 20px;
		margin-left: auto;
		padding: 0;
		background: transparent;
		border: 0;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}

	.fast-toggle:hover {
		background: var(--muted);
		color: var(--foreground);
	}

	.fast-toggle.on {
		background: color-mix(in oklch, var(--primary) 16%, transparent);
		color: var(--primary);
	}

	.fast-toggle.on :global(svg) {
		fill: currentColor;
	}

	.fast-toggle:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
	}

	/* ── Slider ─────────────────────────────────────────────────────────── */
	.slider {
		--thumb: 18px;
		position: relative;
		height: 22px;
	}

	.track,
	.fill {
		position: absolute;
		top: 9px;
		height: 4px;
		border-radius: 2px;
		pointer-events: none;
	}

	.track {
		left: calc(var(--thumb) / 2);
		right: calc(var(--thumb) / 2);
		background: color-mix(in oklch, var(--muted-foreground) 25%, transparent);
	}

	/* `left` and the parent's inset agree with the thumb's travel, so the fill
	   ends exactly under the thumb. */
	.fill {
		left: calc(var(--thumb) / 2);
		max-width: calc(100% - var(--thumb));
		background: var(--primary);
		transition: width 0.12s ease;
	}

	.ticks {
		position: absolute;
		inset: 0 calc(var(--thumb) / 2);
		display: flex;
		justify-content: space-between;
		pointer-events: none;
	}

	.tick {
		width: 2px;
		height: 8px;
		margin-top: 7px;
		border-radius: 1px;
		background: color-mix(in oklch, var(--muted-foreground) 45%, transparent);
	}

	.tick.on {
		background: color-mix(in oklch, var(--primary) 75%, var(--foreground));
	}

	.slider input[type='range'] {
		position: relative;
		z-index: 1;
		display: block;
		width: 100%;
		height: 22px;
		margin: 0;
		appearance: none;
		-webkit-appearance: none;
		background: transparent;
		cursor: pointer;
	}

	.slider input[type='range']::-webkit-slider-runnable-track {
		height: 4px;
		background: transparent;
	}

	.slider input[type='range']::-moz-range-track {
		height: 4px;
		background: transparent;
	}

	.slider input[type='range']::-webkit-slider-thumb {
		width: var(--thumb);
		height: var(--thumb);
		margin-top: -7px;
		appearance: none;
		-webkit-appearance: none;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 3px color-mix(in oklch, #000 35%, transparent);
	}

	.slider input[type='range']::-moz-range-thumb {
		width: 12px;
		height: 12px;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 3px color-mix(in oklch, #000 35%, transparent);
	}

	/* Nothing pinned: the thumb only marks a resting place. */
	.slider.unpinned input[type='range']::-webkit-slider-thumb {
		border-color: var(--subtle-foreground);
	}

	.slider.unpinned input[type='range']::-moz-range-thumb {
		border-color: var(--subtle-foreground);
	}

	.slider input[type='range']:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
		border-radius: 4px;
	}

	/* Zero-width columns spread with space-between put each label's centre on
	   its stop, inset by half the thumb like the track. */
	.stops {
		display: grid;
		/* One label per effective level; the count comes from the registry. */
		justify-content: space-between;
		padding: 0 calc(var(--thumb) / 2);
		margin-top: 4px;
		color: var(--subtle-foreground);
		font-size: 10px;
	}

	.stops span {
		width: max-content;
		justify-self: center;
		white-space: nowrap;
	}

	.stops span.on {
		color: var(--foreground);
	}

	.ends {
		display: flex;
		justify-content: space-between;
		margin-top: 8px;
		color: var(--muted-foreground);
		font-size: 11.5px;
	}

	@media (prefers-reduced-motion: reduce) {
		.pop-value {
			animation: none;
		}
	}
</style>
