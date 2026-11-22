<script lang="ts">
	/**
	 * EffortPopoverCard — the compact reasoning-effort card behind the desktop
	 * composer's effort tag (mockup: docs/design/composer-split-chips.mockup.html,
	 * state 03).
	 *
	 * Header "Effort · <level>" with a model-specific slider, tick marks, the stop
	 * names, and Faster / Smarter end labels. The Fast service tier rides along
	 * in the header when the picked model carries it, so the compact card never
	 * hides a knob the full controls popover offers. Where the route also offers
	 * OpenAI's Ultrafast, the same bolt cycles Standard → Fast (one bolt) →
	 * Ultrafast (two bolts) → Standard; everywhere else it toggles Fast.
	 *
	 * `value === null` means no level is pinned for the chat — the model runs at
	 * its own default. The slider still needs a resting position, so the thumb
	 * sits on the declared default (or first stop) without selecting it; the first
	 * drag pins a level. The header's Auto toggle unpins it again (#1579).
	 */
	import { fastAppliesNotice, speedAppliesNotice } from '$lib/utils/fast-outcome';
	import { SPEED_NAMES, SPEED_OPTIONS, nextSpeed, pickedSpeed, ultrafastCost, type ComposerSpeed, type UltrafastBilling } from './speed-tier';
	import { Zap } from 'lucide-svelte';
	import { NO_ADJUSTABLE_EFFORT, autoEffortLabel } from '$lib/constants/model-effort';

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
		/** A level pins it; `null` returns to Auto (the model's default). */
		onSelect: (value: string | null) => void | Promise<unknown>;
		/** Whether the picked model carries the fast service tier. */
		fastSupported?: boolean;
		fastEnabled?: boolean;
		/** The chat's own Fast setting has not loaded yet (#418). */
		fastPending?: boolean;
		/** The worker said the running turn keeps its speed until the next message. */
		fastWaitsForNextMessage?: boolean;
		onToggleFast?: () => void;
		/** The route also carries Ultrafast: the bolt cycles through three tiers. */
		ultrafastSupported?: boolean;
		/** The chat's Fast runs as Ultrafast. */
		ultrafastEnabled?: boolean;
		/** How the model's Ultrafast is paid for, which words its cost line. */
		ultrafastBilling?: UltrafastBilling;
		/** Settles once the chat has answered the pick. */
		onSelectSpeed?: (speed: ComposerSpeed) => void | Promise<unknown>;
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
		fastPending = false,
		fastWaitsForNextMessage = false,
		onToggleFast,
		ultrafastSupported = false,
		ultrafastEnabled = false,
		ultrafastBilling = 'api',
		onSelectSpeed
	}: Props = $props();

	const speedPicker = $derived(fastSupported && ultrafastSupported);
	const costNote = $derived(ultrafastCost(ultrafastBilling));
	const speed = $derived(pickedSpeed({ fastEnabled, ultrafastSupported, ultrafastEnabled }));
	// The chat answers each pick after the worker saves it. Until every pick
	// has settled, the bolt shows and steps from the newest one asked for, so
	// a quick second click moves on from it instead of repeating it.
	let requested = $state<ComposerSpeed | null>(null);
	let inFlight = 0;
	const boltSpeed = $derived(speedPicker ? (requested ?? speed) : speed);
	const upcoming = $derived(nextSpeed(boltSpeed, speedPicker));
	const boltLabel = $derived(fastPending ? (speedPicker ? 'Speed: loading' : 'Fast mode: loading')
		: speedPicker ? `Speed: ${SPEED_NAMES[boltSpeed]}` : 'Fast mode');
	const boltTitle = $derived.by(() => {
		if (fastPending) return `${speedPicker ? 'Speed' : 'Fast mode'}: loading this chat's own setting`;
		if (!speedPicker) return 'Fast mode: faster output at a higher price. Switching clears the prompt cache.';
		const next = upcoming === 'standard' ? 'Click to go back to Standard.'
			: `Click for ${SPEED_OPTIONS.find((option) => option.value === upcoming)?.title}`;
		return `Speed: ${SPEED_NAMES[boltSpeed]}. ${next}`;
	});

	function clickBolt() {
		if (fastPending) return;
		if (!speedPicker) { onToggleFast?.(); return; }
		const target = upcoming;
		requested = target;
		inFlight++;
		// A refused pick falls back to the tier in force once nothing is pending.
		void Promise.resolve(onSelectSpeed?.(target)).catch(() => {}).finally(() => {
			if (--inFlight === 0) requested = null;
		});
	}

	const pinnedIndex = $derived(options.findIndex((option) => option.value === value));
	const pinned = $derived(pinnedIndex >= 0);
	const maxIndex = $derived(Math.max(options.length - 1, 0));
	const index = $derived(pinned ? pinnedIndex : Math.max(0, options.findIndex(option => option.value === defaultValue)));
	const current = $derived(options[index]);
	const currentLabel = $derived(!known ? (value ?? 'Unknown') : !options.length ? 'Not adjustable' : pinned ? (current?.label ?? 'Effort') : autoEffortLabel(options, defaultValue));
	const progress = $derived(maxIndex > 0 ? (index / maxIndex) * 100 : 0);

	function handleInput(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const option = options[Number(input.value)];
		if (!option || option.value === value) return;
		// The thumb moves before the chat answers. Once it has, put the thumb on
		// the level in force: a refused pick leaves `index` unchanged, so Svelte
		// would not move it back by itself.
		void Promise.resolve(onSelect(option.value)).finally(() => { input.value = String(index); });
	}
</script>

<div class="effort-pop-card" class:fast-active={fastSupported && fastEnabled && !fastPending}
	class:ultrafast-active={speedPicker && speed === 'ultrafast' && !fastPending}>
	<div class="pop-head">
		<span class="pop-title">Effort</span>
		<span class="pop-value-slot">
			{#key currentLabel}
				<span class="pop-value" data-effort-value>{currentLabel}</span>
			{/key}
		</span>
		{#if known && options.length}
			<!-- Always present, so the header never shifts and focus stays put. -->
			<button
				type="button"
				class="auto-toggle"
				aria-pressed={!pinned}
				title={pinned ? "Back to Auto: run at the model's default" : "Auto: the model's default is in use"}
				onclick={() => { if (pinned) onSelect(null); }}
			>Auto</button>
		{/if}
		{#if fastSupported}
			<!-- One bolt for Fast, two for Ultrafast. Where the route carries
			     Ultrafast each click moves one tier on and wraps to Standard. -->
			<button
				type="button"
				class="fast-toggle"
				class:on={boltSpeed !== 'standard' && !fastPending}
				class:pending={fastPending}
				disabled={fastPending}
				onclick={clickBolt}
				aria-pressed={fastPending ? 'mixed' : boltSpeed !== 'standard'}
				aria-busy={fastPending || undefined}
				aria-label={boltLabel}
				title={boltTitle}
				data-speed={fastPending ? undefined : boltSpeed}
				data-testid="speed-bolt"
			>
				<Zap size={13} strokeWidth={2} aria-hidden="true" />
				{#if speedPicker && boltSpeed === 'ultrafast' && !fastPending}<Zap size={13} strokeWidth={2} aria-hidden="true" />{/if}
			</button>
		{/if}
	</div>

	{#if speedPicker && boltSpeed === 'ultrafast' && !fastPending && costNote}
		<p class="speed-cost" data-testid="ultrafast-cost">{costNote}</p>
	{/if}

	<div aria-live="polite">
		{#if fastSupported && fastWaitsForNextMessage}
			<p class="fast-applies" data-testid="fast-applies-notice">{speedPicker ? speedAppliesNotice(speed) : fastAppliesNotice(fastEnabled)}</p>
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
	.fast-applies {
		margin: 0 0 10px;
		font-size: 12px;
		line-height: 1.4;
		color: var(--muted-foreground);
	}
	.single-effort { padding: 6px 12px; border-radius: 6px; background: var(--muted); color: var(--foreground); }
	.single-effort:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.effort-pop-card {
		padding: 12px 14px;
		min-width: 0;
		position: relative;
		isolation: isolate;
		border-radius: inherit;
	}

	/* Decorative energy stays behind the controls and never catches a click.
	   Three short passes celebrate activation, then settle to a quiet glow. */
	.effort-pop-card::before,
	.effort-pop-card::after {
		content: '';
		position: absolute;
		inset: 0;
		z-index: -1;
		border-radius: inherit;
		pointer-events: none;
		opacity: 0;
	}

	.fast-active::before {
		background: radial-gradient(ellipse at top right, color-mix(in oklch, var(--primary) 13%, transparent), transparent 75%);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 28%, transparent);
		opacity: 1;
	}

	.fast-active::after {
		background: linear-gradient(110deg, transparent 35%, color-mix(in oklch, var(--primary) 12%, transparent) 49%, color-mix(in oklch, var(--primary) 24%, transparent) 50%, transparent 65%);
		background-size: 300% 100%;
		animation: fast-energy-sweep 1.6s ease-out 3;
	}

	/* Ultrafast: the same sweep, quicker. */
	.ultrafast-active::after {
		animation-duration: 1s;
	}

	@keyframes fast-energy-sweep {
		0% { background-position: 100% 0; opacity: 0; }
		15% { opacity: 1; }
		70% { background-position: 0% 0; opacity: 0; }
		100% { background-position: 0% 0; opacity: 0; }
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

	.auto-toggle {
		margin-left: auto;
		padding: 1px 8px;
		border: 0;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 12px;
		line-height: 18px;
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}

	.auto-toggle:hover {
		background: var(--muted);
		color: var(--foreground);
	}

	.auto-toggle[aria-pressed='true'] {
		background: color-mix(in oklab, var(--primary) 14%, var(--card));
		color: var(--primary);
		cursor: default;
	}

	.auto-toggle:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
	}

	/* The Fast toggle sits right after Auto instead of pushing itself right. */
	.auto-toggle + .fast-toggle { margin-left: 0; }

	.fast-toggle {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 20px;
		height: 20px;
		margin-left: auto;
		padding: 0 3px;
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
		background: color-mix(in oklab, var(--primary) 18%, var(--card));
		color: var(--primary);
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	.fast-toggle.on :global(svg) {
		fill: currentColor;
		animation: fast-bolt-charge 480ms ease-out;
	}

	/* The click always lands on the button itself. Leaving Ultrafast removes
	   the second bolt mid-click, and the dock's click-outside check would read
	   a detached target as a click outside the popover and close it. */
	.fast-toggle :global(svg) {
		pointer-events: none;
	}

	/* Ultrafast: the second bolt tucks behind the first. */
	.fast-toggle :global(svg + svg) {
		margin-left: -5px;
	}

	@keyframes fast-bolt-charge {
		0% { transform: scale(0.75) rotate(12deg); }
		55% { transform: scale(1.18) rotate(-8deg); }
		100% { transform: scale(1) rotate(0); }
	}

	/* Waiting for the chat's own value (#418): neither on nor off. */
	.fast-toggle.pending {
		cursor: progress;
		color: var(--subtle-foreground, var(--muted-foreground));
		animation: fast-pending 1.4s ease-in-out infinite;
	}

	.fast-toggle.pending:hover {
		background: transparent;
	}

	@keyframes fast-pending {
		0%, 100% { opacity: 0.4; }
		50% { opacity: 0.9; }
	}

	.fast-toggle:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
	}

	/* Ultrafast's cost, said while it is the picked tier. */
	.speed-cost {
		margin: -6px 0 12px;
		font-size: 11.5px;
		line-height: 1.4;
		color: var(--muted-foreground);
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
		background: color-mix(in oklab, var(--primary) 75%, var(--foreground));
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
		.pop-value,
		.fast-active::after,
		.fast-toggle.on :global(svg),
		.fast-toggle.pending {
			animation: none;
		}

		.fast-toggle,
		.auto-toggle,
		.fill {
			transition: none;
		}
	}
</style>
