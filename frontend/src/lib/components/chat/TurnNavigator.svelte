<script lang="ts">
	/** Left-rail user-turn minimap. Each tick is a turn, on the prompt that
	 *  opened it (a mid-turn steer is not a stop of its own); hover reveals the
	 *  message. Stays in the transcript gutter and never touches the composer.
	 *  One rail at every history length: when the ticks outgrow the rail it
	 *  scrolls (wheel, keyboard, or a touch scrub held at its edge) instead of
	 *  switching to another UI.
	 *
	 *  Touch has no hover, so sliding a finger along the rail scrubs it: the
	 *  tick under the finger lights up and shows its preview like a mouse hover,
	 *  and the transcript jumps to it once the finger rests on it or lifts. */
	import type { UserTurnNavigationItem } from '$lib/utils/transcript-navigation';
	import { resolveTurnNavigationIndex } from '$lib/utils/transcript-navigation';
	import { tick as nextTick } from 'svelte';

	interface Props {
		items: UserTurnNavigationItem[];
		currentIndex: number;
		onSelect: (index: number) => void;
		/** Turns waiting on the reader (spec §5 "Needs you"): their ticks
		 *  carry the amber dot the sidebar's Needs-you rows use. */
		needsYouIds?: readonly string[];
	}

	let { items, currentIndex, onSelect, needsYouIds = [] }: Props = $props();
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
	// never undone by a new turn arriving. A touch scrub holds the rail still:
	// moving it under the finger would change which tick the finger is on.
	$effect(() => {
		const index = resolvedIndex;
		const key = `${items[0]?.id}:${index}`;
		if (!rail || key === revealedKey || scrubbing) return;
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

	// ── Touch scrub ─────────────────────────────────────────────────────────
	/** Movement that turns a touch into a scrub; less is a tap (the click path). */
	const SCRUB_SLOP_PX = 4;
	/** How long the finger rests on one tick before the transcript follows it. */
	const SCRUB_DWELL_MS = 300;
	/** Band at each rail edge where a held finger scrolls the rail. */
	const SCRUB_EDGE_PX = 28;
	/** Fastest edge scroll, per 60 Hz frame (scaled by the real frame time so
	 *  a 120 Hz phone scrolls at the same speed). */
	const SCRUB_EDGE_MAX_STEP_PX = 12;
	const FRAME_MS = 1000 / 60;

	interface Scrub {
		pointerId: number;
		startY: number;
		y: number;
		active: boolean;
		/** Last index handed to onSelect during this gesture. */
		selected: number | null;
		/** Tick the finger landed on, whose click a scrub must swallow. */
		startTick: number;
		/** Timestamp of the previous edge-scroll frame (0: not scrolling). */
		lastFrame: number;
	}
	let scrub: Scrub | null = null;
	let scrubbing = $state(false);
	let dwellTimer: ReturnType<typeof setTimeout> | undefined;
	let edgeFrame = 0;
	let suppressClick = { index: -1, until: 0 };

	/** The tick nearest a viewport y, clamped to the rail's visible box. */
	function indexAtY(clientY: number): number | null {
		if (!rail || items.length === 0) return null;
		const box = rail.getBoundingClientRect();
		const y = Math.min(Math.max(clientY, box.top), box.bottom) - box.top + rail.scrollTop;
		let lo = 0;
		let hi = items.length - 1;
		while (lo < hi) {
			const mid = (lo + hi + 1) >> 1;
			if ((tickAt(mid)?.offsetTop ?? 0) <= y) lo = mid;
			else hi = mid - 1;
		}
		const tickEl = tickAt(lo);
		const next = tickAt(lo + 1);
		if (tickEl && next && y - (tickEl.offsetTop + tickEl.offsetHeight) > next.offsetTop - y) return lo + 1;
		return lo;
	}

	function selectFromScrub(index: number) {
		if (!scrub || scrub.selected === index) return;
		scrub.selected = index;
		onSelect(index);
	}

	function trackScrub() {
		if (!scrub?.active) return;
		const index = indexAtY(scrub.y);
		if (index !== null && index !== hoveredIndex) {
			showPreview(index);
			clearTimeout(dwellTimer);
			dwellTimer = setTimeout(() => selectFromScrub(index), SCRUB_DWELL_MS);
		}
		if (!edgeFrame && typeof requestAnimationFrame === 'function') edgeFrame = requestAnimationFrame(edgeScroll);
	}

	/** Held near (or past) an edge with more turns beyond it, the rail scrolls
	 *  under the finger, faster the further out the finger is. */
	function edgeScroll(now: number) {
		edgeFrame = 0;
		if (!scrub?.active || !rail) return;
		const box = rail.getBoundingClientRect();
		const intoTop = box.top + SCRUB_EDGE_PX - scrub.y;
		const intoBottom = scrub.y - (box.bottom - SCRUB_EDGE_PX);
		const frames = scrub.lastFrame ? Math.min(3, (now - scrub.lastFrame) / FRAME_MS) : 1;
		const step = (depth: number) =>
			Math.ceil(Math.min(1, depth / (SCRUB_EDGE_PX * 2)) * SCRUB_EDGE_MAX_STEP_PX * frames);
		let delta = 0;
		if (intoTop > 0 && rail.scrollTop > 0) delta = -step(intoTop);
		else if (intoBottom > 0 && rail.scrollTop + rail.clientHeight < rail.scrollHeight) delta = step(intoBottom);
		if (!delta) {
			scrub.lastFrame = 0;
			return;
		}
		scrub.lastFrame = now;
		rail.scrollTop += delta;
		updateScrollEdges();
		trackScrub();
	}

	function endScrub(commit: boolean) {
		const active = scrub?.active ?? false;
		const startTick = scrub?.startTick ?? -1;
		clearTimeout(dwellTimer);
		if (edgeFrame && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(edgeFrame);
		edgeFrame = 0;
		if (active && commit && hoveredIndex !== null) selectFromScrub(hoveredIndex);
		scrub = null;
		if (!active) return;
		scrubbing = false;
		hoveredIndex = null;
		// The gesture's own click (if the browser still sends one) must not
		// select the tick the finger started on. Other ticks stay tappable.
		suppressClick = { index: startTick, until: Date.now() + 400 };
		// Like a tap, a scrub must not leave a focused tick lit as if current.
		const focusedEl = document.activeElement;
		if (focusedEl instanceof HTMLElement && nav?.contains(focusedEl)) focusedEl.blur();
	}

	function onRailPointerDown(event: PointerEvent) {
		if (event.pointerType === 'mouse' || event.isPrimary === false) return;
		endScrub(false);
		const landed = event.target instanceof Element ? event.target.closest('.tick') : null;
		scrub = {
			pointerId: event.pointerId,
			startY: event.clientY,
			y: event.clientY,
			active: false,
			selected: null,
			startTick: landed && rail ? Array.prototype.indexOf.call(rail.children, landed) : -1,
			lastFrame: 0
		};
	}

	function onRailPointerMove(event: PointerEvent) {
		if (!scrub || event.pointerId !== scrub.pointerId) return;
		scrub.y = event.clientY;
		if (!scrub.active) {
			if (Math.abs(scrub.y - scrub.startY) < SCRUB_SLOP_PX) return;
			scrub.active = true;
			scrubbing = true;
			// Capture on the rail so the finger can leave it (to scroll at an
			// edge) and the release no longer reads as a tap on the first tick.
			try {
				rail?.setPointerCapture(event.pointerId);
			} catch {
				// Pointer already gone; pointerup/cancel ends the scrub.
			}
		}
		event.preventDefault();
		trackScrub();
	}

	function onRailPointerEnd(event: PointerEvent) {
		if (!scrub || event.pointerId !== scrub.pointerId) return;
		endScrub(event.type === 'pointerup');
	}

	$effect(() => () => endScrub(false));

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
	class:is-scrubbing={scrubbing}
	style="--count: {items.length}"
	aria-label="Navigate user turns"
	onfocusin={() => (focused = true)}
	onfocusout={() => (focused = false)}
	onmouseleave={() => (hoveredIndex = null)}
	onpointerdown={(event) => {
		lastPointerType = event.pointerType;
		onRailPointerDown(event);
	}}
	onpointermove={onRailPointerMove}
	onpointerup={onRailPointerEnd}
	onpointercancel={onRailPointerEnd}
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
				aria-label="Turn {index + 1} of {items.length}: {item.label}{needsYouIds.includes(item.id) ? ' (needs you)' : ''}"
				aria-current={index === resolvedIndex ? 'true' : undefined}
				tabindex={index === (focusedTick ?? resolvedIndex) ? 0 : -1}
				onclick={(event) => {
					if (index === suppressClick.index && Date.now() < suppressClick.until) return;
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
				{#if needsYouIds.includes(item.id)}
					<span class="ml-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-warning" data-needs-you aria-hidden="true"></span>
				{/if}
			</button>
		{/each}
	</div>
	{#if previewItem}
		<span
			class="preview"
			class:is-visible={hoveredIndex !== null}
			class:is-above={scrubbing}
			style="top: {previewTop}px" aria-hidden="true">
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
		/* A finger on the rail scrubs it (see the script) rather than panning;
		   holding at an edge scrolls it. Wheel and trackpad are unaffected. */
		touch-action: none;
		-webkit-user-select: none;
		user-select: none;
		-webkit-touch-callout: none;
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

	/* During a touch scrub the thumb covers the space beside the tick, so the
	   preview rides above the finger instead. */
	.preview.is-above {
		transform: translate(-8px, calc(-100% - 20px)) scale(0.98);
		transform-origin: left bottom;
	}

	.preview.is-above.is-visible {
		transform: translate(0, calc(-100% - 28px)) scale(1);
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

	/* A touch scrub is the touch stand-in for hover: the same dimming around
	   the tick under the finger. */
	.turn-nav.is-scrubbing .tick:not(.is-hovered):not(.is-current) .bar {
		opacity: calc(0.42 + var(--proximity, 0) * 0.58);
	}

	/* Touch browsers can hold :hover on the tick the finger landed on; only
	   the tick under the finger should read as lit. */
	.turn-nav.is-scrubbing .tick:hover:not(.is-hovered):not(.is-current) .bar {
		height: 2px;
		background: color-mix(in oklch, var(--subtle-foreground) 72%, transparent);
		transform: translateX(calc(var(--proximity, 0) * 4px)) scaleX(calc(1 + var(--proximity, 0) * 0.8));
	}

	/* No hover on touch: a tap navigates, so a preview or ripple would only
	   linger over the transcript the tap just moved. A scrub shows both while
	   the finger is down and drops them when it lifts. */
	@media (hover: none) {
		.turn-nav:not(.is-scrubbing) .preview {
			display: none;
		}

		.turn-nav:not(.is-scrubbing) .tick .bar,
		.turn-nav:not(.is-scrubbing) .tick:hover .bar,
		.turn-nav:not(.is-scrubbing) .tick.is-hovered .bar {
			transform: none;
		}

		/* Touch browsers keep :hover on the last tapped tick. Without this it
		   stays lit and dims the rest, so it reads as the current turn after
		   the reader scrolls away from it. */
		.turn-nav:not(.is-scrubbing):hover .tick:not(:hover):not(.is-current) .bar {
			opacity: 1;
		}

		.turn-nav:not(.is-scrubbing) .tick:hover:not(.is-current) .bar {
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
