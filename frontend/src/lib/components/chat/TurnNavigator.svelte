<script lang="ts">
	/** Left-rail user-turn minimap. Each tick is a prompt; hover reveals the
	 *  message. Stays in the transcript gutter and never touches the composer.
	 *  One rail at every history length: when the ticks outgrow the rail it
	 *  scrolls (wheel, touch, keyboard) instead of switching to another UI. */
	import type { UserTurnNavigationItem } from '$lib/utils/transcript-navigation';
	import { resolveTurnNavigationIndex } from '$lib/utils/transcript-navigation';
	import { tick as nextTick } from 'svelte';

	interface Props {
		items: UserTurnNavigationItem[];
		currentIndex: number;
		onSelect: (index: number) => void;
	}

	let { items, currentIndex, onSelect }: Props = $props();
	let focused = $state(false);
	let hoveredIndex = $state<number | null>(null);
	// Roving tab stop: one tick is in the Tab order (the focused one, else the
	// current turn) so a long history is not hundreds of Tab presses to cross.
	// Arrows, Home and End move between ticks.
	let focusedTick = $state<number | null>(null);
	let lastPointerType = '';
	// The preview lives outside the scrolling rail so the rail's overflow
	// cannot clip it; it keeps the last item while it fades out.
	let previewIndex = $state(0);
	let previewTop = $state(0);
	let canScrollUp = $state(false);
	let canScrollDown = $state(false);
	let nav = $state<HTMLElement>();
	let rail = $state<HTMLElement>();
	const resolvedIndex = $derived(Math.max(0, Math.min(items.length - 1, currentIndex)));
	const previewItem = $derived(items[previewIndex] ?? null);
	// Plain variable: revealing must not re-run when only the rail scrolls.
	let revealedKey = '';

	function tickAt(index: number): HTMLElement | undefined {
		return rail?.children[index] as HTMLElement | undefined;
	}

	/** Scroll only the rail (never the transcript) so the tick is visible with
	 *  a couple of ticks of context on each side. */
	function revealTick(index: number) {
		const target = tickAt(index);
		if (!rail || !target) return;
		const margin = target.offsetHeight * 2;
		const top = target.offsetTop - margin;
		const bottom = target.offsetTop + target.offsetHeight + margin;
		if (top < rail.scrollTop) rail.scrollTop = Math.max(0, top);
		else if (bottom > rail.scrollTop + rail.clientHeight) rail.scrollTop = bottom - rail.clientHeight;
		updateScrollEdges();
	}

	function updateScrollEdges() {
		if (!rail) return;
		canScrollUp = rail.scrollTop > 1;
		canScrollDown = rail.scrollTop + rail.clientHeight < rail.scrollHeight - 1;
	}

	function positionPreview() {
		const target = hoveredIndex === null ? undefined : tickAt(hoveredIndex);
		if (!nav || !target) return;
		const box = target.getBoundingClientRect();
		previewTop = box.top + box.height / 2 - nav.getBoundingClientRect().top;
	}

	function showPreview(index: number) {
		hoveredIndex = index;
		previewIndex = index;
		positionPreview();
	}

	function onRailScroll() {
		updateScrollEdges();
		positionPreview();
	}

	// Keep the reading position's tick in view as the transcript moves. Only a
	// change of position (or of session) scrolls the rail, so browsing it is
	// never undone by a new turn arriving.
	$effect(() => {
		const index = resolvedIndex;
		const key = `${items[0]?.id}:${index}`;
		if (!rail || key === revealedKey) return;
		revealedKey = key;
		revealTick(index);
	});

	// Edge fades track the rail's size and its tick count.
	$effect(() => {
		void items.length;
		if (rail) void nextTick().then(updateScrollEdges);
	});

	$effect(() => {
		if (!rail || typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(updateScrollEdges);
		observer.observe(rail);
		return () => observer.disconnect();
	});

	function onKeydown(event: KeyboardEvent, index: number) {
		let destination: number;
		if (event.key === 'Home' || event.key === 'End') {
			destination = event.key === 'Home' ? 0 : items.length - 1;
		} else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
			destination = resolveTurnNavigationIndex(index, items.length, event.key === 'ArrowUp' ? -1 : 1);
		} else {
			return;
		}
		event.preventDefault();
		// Focus follows the selection so the next key starts from the tick the
		// reader can see; the rail scrolls itself, not the page.
		tickAt(destination)?.focus({ preventScroll: true });
		revealTick(destination);
		if (event.key === 'Home' || event.key === 'End' || destination !== resolvedIndex) onSelect(destination);
	}
</script>

<nav
	bind:this={nav}
	class="turn-nav"
	class:is-focused={focused}
	style="--count: {items.length}"
	aria-label="Navigate user turns"
	onfocusin={() => (focused = true)}
	onfocusout={() => (focused = false)}
	onmouseleave={() => (hoveredIndex = null)}
	onpointerdown={(event) => (lastPointerType = event.pointerType)}
>
	<div
		bind:this={rail}
		class="turn-rail"
		class:can-scroll-up={canScrollUp}
		class:can-scroll-down={canScrollDown}
		onscroll={onRailScroll}
	>
		{#each items as item, index (item.id)}
			<button
				type="button"
				class="tick"
				class:is-current={index === resolvedIndex}
				class:is-hovered={hoveredIndex === index}
				style="--proximity: {hoveredIndex === null ? 0 : Math.max(0, 1 - Math.abs(index - hoveredIndex) / 3)}"
				aria-label="User message {index + 1} of {items.length}: {item.label}"
				aria-current={index === resolvedIndex ? 'true' : undefined}
				tabindex={index === (focusedTick ?? resolvedIndex) ? 0 : -1}
				onclick={(event) => {
					onSelect(index);
					// A tap focuses the tick, which would keep it lit as if it were
					// the current turn after the reader scrolls on.
					if (lastPointerType === 'touch') event.currentTarget.blur();
				}}
				onkeydown={(event) => onKeydown(event, index)}
				onmouseenter={() => showPreview(index)}
				onfocus={() => {
					focusedTick = index;
					showPreview(index);
				}}
				onblur={() => {
					if (focusedTick === index) focusedTick = null;
					if (hoveredIndex === index) hoveredIndex = null;
				}}
			>
				<span class="bar" aria-hidden="true"></span>
			</button>
		{/each}
	</div>
	{#if previewItem}
		<span class="preview" class:is-visible={hoveredIndex !== null} style="top: {previewTop}px" aria-hidden="true">
			<span class="preview-title">{previewItem.title}</span>
			{#if previewItem.preview}
				<span class="preview-body">{previewItem.preview}</span>
			{/if}
		</span>
	{/if}
</nav>

<style>
	.turn-nav {
		position: absolute;
		left: 0;
		top: 50%;
		z-index: 12;
		display: flex;
		flex-direction: column;
		transform: translateY(-50%);
		pointer-events: auto;
	}

	/* Ticks start at the top and never shrink: a centred, shrinking column is
	   what pushed the first turns off-screen in long sessions (#1378). */
	.turn-rail {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: clamp(3px, calc(42vh / var(--count, 4) - 2px), 9px);
		max-height: 56vh;
		min-height: 0;
		/* The 2px left pad (the nav sits at left: 0) keeps the focus ring
		   clear of the rail's horizontal clip. */
		padding: 10px 12px 10px 2px;
		overflow-x: hidden;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-width: none;
		--fade-top: 0px;
		--fade-bottom: 0px;
		mask-image: linear-gradient(
			to bottom,
			transparent 0,
			#000 var(--fade-top),
			#000 calc(100% - var(--fade-bottom)),
			transparent 100%
		);
	}

	.turn-rail::-webkit-scrollbar {
		display: none;
	}

	/* Fade the edge that has more turns beyond it. */
	.turn-rail.can-scroll-up {
		--fade-top: 24px;
	}

	.turn-rail.can-scroll-down {
		--fade-bottom: 24px;
	}

	.tick {
		position: relative;
		pointer-events: auto;
		display: flex;
		flex-shrink: 0;
		align-items: center;
		width: 28px;
		height: 10px;
		padding: 0;
		border: 0;
		background: transparent;
		cursor: pointer;
	}

	.bar {
		display: block;
		height: 2px;
		width: 14px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--subtle-foreground) 72%, transparent);
		transform: translateX(calc(var(--proximity, 0) * 4px)) scaleX(calc(1 + var(--proximity, 0) * 0.8));
		transform-origin: left center;
		transition:
			background-color 180ms ease,
			transform 240ms cubic-bezier(0.22, 1, 0.36, 1),
			opacity 160ms ease;
	}

	.turn-nav:hover .tick:not(:hover):not(.is-current) .bar,
	.turn-nav.is-focused .tick:not(:focus-visible):not(.is-current) .bar {
		opacity: calc(0.42 + var(--proximity, 0) * 0.58);
	}

	.tick.is-current .bar {
		background: color-mix(in oklch, var(--foreground) 82%, transparent);
	}

	.tick:hover .bar,
	.tick.is-hovered .bar,
	.tick:focus-visible .bar {
		background: var(--foreground);
		height: 3px;
		transform: translateX(0) scaleX(2.2);
	}

	.tick:focus-visible {
		outline: none;
	}

	.tick:focus-visible .bar {
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 70%, transparent);
	}

	.preview {
		position: absolute;
		left: 40px;
		width: min(320px, calc(100vw - 96px));
		padding: 10px 12px;
		border: 1px solid color-mix(in oklch, var(--border) 88%, transparent);
		border-radius: 12px;
		background: color-mix(in oklch, var(--popover) 96%, transparent);
		box-shadow: var(--shadow-m);
		backdrop-filter: var(--fx-rail-backdrop);
		-webkit-backdrop-filter: var(--fx-rail-backdrop);
		opacity: 0;
		pointer-events: none;
		transform: translate(-8px, -50%) scale(0.98);
		transform-origin: left center;
		transition:
			opacity 160ms ease,
			transform 180ms cubic-bezier(0.22, 1, 0.36, 1);
		z-index: 2;
		text-align: left;
	}

	.preview.is-visible {
		opacity: 1;
		transform: translate(0, -50%) scale(1);
	}

	.preview-title {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13px;
		font-weight: 600;
		letter-spacing: -0.015em;
		line-height: 1.3;
		color: var(--foreground);
	}

	.preview-body {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		margin-top: 4px;
		font-size: 12px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}

	/* Compact and touch layouts: a narrower rail with taller tap targets. */
	@media (max-width: 639px), (pointer: coarse) {
		.turn-rail {
			gap: 0;
			padding-right: 4px;
		}

		.tick {
			width: 20px;
			height: 16px;
		}

		.bar {
			width: 12px;
		}

		/* A smaller stretch so the hovered or focused bar fits the narrow rail. */
		.tick:hover .bar,
		.tick.is-hovered .bar,
		.tick:focus-visible .bar {
			transform: translateX(0) scaleX(1.6);
		}
	}

	/* No hover on touch: a tap navigates, so a preview or ripple would only
	   linger over the transcript the tap just moved. */
	@media (hover: none) {
		.preview {
			display: none;
		}

		.tick .bar,
		.tick:hover .bar,
		.tick.is-hovered .bar {
			transform: none;
		}

		/* Touch browsers keep :hover on the last tapped tick. Without this it
		   stays lit and dims the rest, so it reads as the current turn after
		   the reader scrolls away from it. */
		.turn-nav:hover .tick:not(:hover):not(.is-current) .bar {
			opacity: 1;
		}

		.tick:hover:not(.is-current) .bar {
			height: 2px;
			background: color-mix(in oklch, var(--subtle-foreground) 72%, transparent);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.bar,
		.preview { transition: none; }
		.bar { transform: none; }
	}
</style>
