<script lang="ts" module>
	export interface DetailTab {
		id: string;
		label?: string;
		count?: number | string;
		disabled?: boolean;
		title?: string;
	}

	/** Extra room kept beside the active tab when it is scrolled into view. */
	export const TAB_REVEAL_PAD = 32;

	/**
	 * The scrollLeft that shows `tab` with some room on each side, or null when
	 * it already fits. Pure, so the strip's behaviour is testable without layout.
	 */
	export function revealScrollLeft(
		tab: { left: number; width: number },
		view: { scrollLeft: number; width: number },
		pad = TAB_REVEAL_PAD
	): number | null {
		const right = tab.left + tab.width;
		if (tab.left - pad < view.scrollLeft) return Math.max(0, tab.left - pad);
		if (right + pad > view.scrollLeft + view.width) return right + pad - view.width;
		return null;
	}

	/** Gap between tabs, and the tighter gap used only when the wide one would overflow. */
	export const TAB_GAP = 20;
	export const TAB_GAP_TIGHT = 12;

	/** The gap to use: the wide one unless the tabs only fit with the tight one. */
	export function tabGap(widths: number[], available: number): number {
		const total = widths.reduce((a, b) => a + b, 0);
		const gaps = Math.max(0, widths.length - 1);
		return total + gaps * TAB_GAP > available ? TAB_GAP_TIGHT : TAB_GAP;
	}

	/**
	 * Indexes of the tabs that are only partly inside the visible strip. At rest
	 * they are hidden, so a label is never shown cut mid-word: a tab is either
	 * whole or fully past the edge (still reachable by scrolling).
	 */
	export function partialTabs(
		tabs: { left: number; width: number }[],
		view: { scrollLeft: number; width: number },
		slack = 1
	): number[] {
		const lo = view.scrollLeft - slack;
		const hi = view.scrollLeft + view.width + slack;
		const out: number[] = [];
		tabs.forEach((t, i) => {
			const r = t.left + t.width;
			const inside = t.left >= lo && r <= hi;
			const outside = r <= lo || t.left >= hi;
			if (!inside && !outside) out.push(i);
		});
		return out;
	}
</script>

<script lang="ts">
	/**
	 * DetailTabs: tabs of one detail (role=tablist). Arrow keys, Home and End
	 * move and select. DetailPane renders it only when there are 2+ tabs.
	 *
	 * When the tabs do not fit, the gap first tightens from 20 to 12px; if they
	 * still do not fit, the strip scrolls sideways: touch and trackpad natively,
	 * a vertical mouse wheel turns into sideways scroll, a mouse can drag it.
	 * Scrolling snaps to whole tabs, the edge with more tabs fades, and the
	 * trailing chevron has its own reserved slot. At rest a tab that does not
	 * fit whole is hidden, never shown cut. The selected tab is always kept in
	 * view (after selection, keyboard moves and resizes).
	 */
	import { tick } from 'svelte';
	import { ChevronRight } from 'lucide-svelte';

	interface Props {
		tabs: DetailTab[];
		current: string | null;
		onselect: (id: string) => void;
		/** Accessible name, e.g. "Claude Max subscription sections". */
		label: string;
		/** Prefix for tab/panel ids. */
		idPrefix: string;
	}

	let { tabs, current, onselect, label, idPrefix }: Props = $props();
	let list: HTMLDivElement | undefined = $state();
	let wrap: HTMLDivElement | undefined = $state();
	let moreLeft = $state(false);
	let moreRight = $state(false);
	/** The tabs overflow the row: reserve the chevron slot. */
	let overflowing = $state(false);
	let tight = $state(false);
	/** Ids of tabs that are only partly visible (hidden at rest). */
	let partial = $state<string[]>([]);
	let scrolling = $state(false);
	let restTimer: ReturnType<typeof setTimeout> | undefined;
	let dragging = $state(false);
	let drag: { x: number; left: number; moved: boolean; id: number } | null = null;
	/** A drag ends with a click on the tab under the pointer; swallow it. */
	let dragEndedAt = 0;

	function tabEls(): HTMLElement[] {
		return list ? Array.from(list.querySelectorAll<HTMLElement>('[data-tab-id]')) : [];
	}

	/** Pick the gap and whether the chevron slot is needed, from the row's own width. */
	function measure() {
		if (!list || !wrap) return;
		const els = tabEls();
		const avail = wrap.clientWidth;
		if (!els.length || !avail) return;
		tight = tabGap(els.map((e) => e.offsetWidth), avail) === TAB_GAP_TIGHT;
		const gap = tight ? TAB_GAP_TIGHT : TAB_GAP;
		const natural = els.reduce((a, e) => a + e.offsetWidth, 0) + gap * (els.length - 1);
		overflowing = natural > avail + 0.5;
	}

	function updateFades() {
		if (!list) return;
		const max = list.scrollWidth - list.clientWidth;
		moreLeft = list.scrollLeft > 2;
		moreRight = max > 2 && list.scrollLeft < max - 2;
		const els = tabEls();
		const idx = partialTabs(
			els.map((e) => ({ left: e.offsetLeft - list!.offsetLeft, width: e.offsetWidth })),
			{ scrollLeft: list.scrollLeft, width: list.clientWidth }
		);
		const ids = idx.map((i) => els[i].dataset.tabId ?? '');
		if (ids.join() !== partial.join()) partial = ids;
	}

	/** While the strip moves every tab shows; once it rests, cut tabs hide again. */
	function onscroll() {
		scrolling = true;
		updateFades();
		clearTimeout(restTimer);
		restTimer = setTimeout(() => { scrolling = false; updateFades(); }, 140);
	}

	function reveal(id: string | null) {
		if (!list || !id) return;
		const el = tabEls().find((t) => t.dataset.tabId === id);
		if (!el) return;
		const next = revealScrollLeft(
			{ left: el.offsetLeft - list.offsetLeft, width: el.offsetWidth },
			{ scrollLeft: list.scrollLeft, width: list.clientWidth }
		);
		if (next !== null) list.scrollLeft = next;
		updateFades();
	}

	// Keep the selected tab visible whenever it changes.
	$effect(() => {
		const id = current;
		void tick().then(() => reveal(id));
	});

	// ...and when the row changes size (or the tab labels/counts change).
	$effect(() => {
		void tabs;
		if (!list || !wrap) return;
		measure();
		void tick().then(() => reveal(current));
		if (typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => { measure(); void tick().then(() => reveal(current)); });
		ro.observe(wrap);
		return () => { ro.disconnect(); clearTimeout(restTimer); };
	});

	function onkeydown(event: KeyboardEvent) {
		const enabled = tabs.filter((t) => !t.disabled);
		if (!enabled.length) return;
		const i = enabled.findIndex((t) => t.id === current);
		let next = -1;
		if (event.key === 'ArrowRight') next = (i + 1) % enabled.length;
		else if (event.key === 'ArrowLeft') next = (i - 1 + enabled.length) % enabled.length;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = enabled.length - 1;
		if (next < 0) return;
		event.preventDefault();
		onselect(enabled[next].id);
		list?.querySelector<HTMLElement>(`[data-tab-id="${enabled[next].id}"]`)?.focus({ preventScroll: true });
		reveal(enabled[next].id);
	}

	/** A vertical wheel scrolls the strip sideways, until it reaches an end. */
	function onwheel(event: WheelEvent) {
		if (!list) return;
		const max = list.scrollWidth - list.clientWidth;
		if (max <= 0) return;
		const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
		if ((delta < 0 && list.scrollLeft <= 0) || (delta > 0 && list.scrollLeft >= max)) return;
		event.preventDefault();
		list.scrollLeft += delta;
		updateFades();
	}

	function onpointerdown(event: PointerEvent) {
		if (event.pointerType !== 'mouse' || event.button !== 0 || !list) return;
		if (list.scrollWidth <= list.clientWidth) return;
		drag = { x: event.clientX, left: list.scrollLeft, moved: false, id: event.pointerId };
	}

	function onpointermove(event: PointerEvent) {
		if (!drag || !list) return;
		const dx = event.clientX - drag.x;
		if (!drag.moved && Math.abs(dx) > 5) {
			drag.moved = true;
			dragging = true;
			try {
				list.setPointerCapture(drag.id);
			} catch {
				// Capture is best effort (synthetic events in tests).
			}
		}
		if (drag.moved) list.scrollLeft = drag.left - dx;
	}

	function endDrag() {
		if (drag?.moved) dragEndedAt = Date.now();
		drag = null;
		dragging = false;
	}

	function choose(id: string) {
		if (Date.now() - dragEndedAt < 250) return;
		onselect(id);
	}

	// Wheel must be non-passive to stop the page scrolling while the strip moves.
	$effect(() => {
		const el = list;
		if (!el) return;
		el.addEventListener('wheel', onwheel, { passive: false });
		return () => el.removeEventListener('wheel', onwheel);
	});
</script>

<div class="st-tabs-wrap" class:more-l={moreLeft} class:more-r={moreRight} class:overflowing class:tight bind:this={wrap}>
	<div
		class="st-tabs"
		class:dragging
		class:scrolling
		role="tablist"
		aria-label={label}
		bind:this={list}
		tabindex="-1"
		{onkeydown}
		{onscroll}
		{onpointerdown}
		{onpointermove}
		onpointerup={endDrag}
		onpointercancel={endDrag}
	>
		{#each tabs as tab (tab.id)}
			<button
				type="button"
				role="tab"
				id="{idPrefix}-tab-{tab.id}"
				data-tab-id={tab.id}
				data-cut={!scrolling && !dragging && tab.id !== current && partial.includes(tab.id) ? '' : undefined}
				disabled={tab.disabled}
				aria-disabled={tab.disabled || undefined}
				title={tab.title}
				aria-selected={tab.id === current}
				aria-controls="{idPrefix}-panel"
				tabindex={tab.id === current ? 0 : -1}
				onclick={() => choose(tab.id)}
			>
				{tab.label ?? tab.id}{#if tab.count !== undefined && tab.count !== ''}<span class="n">{tab.count}</span>{/if}
			</button>
		{/each}
	</div>
	{#if moreRight}<span class="tabs-more" aria-hidden="true"><ChevronRight size={12} strokeWidth={1.8} /></span>{/if}
</div>

<style>
	/* The baseline spans the whole row, chevron slot included. */
	.st-tabs-wrap {
		--tab-gap: 20px;
		--tab-fade: 24px;
		--tab-slot: 28px;
		position: relative;
		display: flex;
		margin-top: 14px;
		border-bottom: 1px solid var(--st-border);
	}
	.st-tabs-wrap.tight { --tab-gap: 12px; }
	.st-tabs {
		flex: 1;
		min-width: 0;
		display: flex;
		gap: var(--tab-gap);
		overflow-x: auto;
		overscroll-behavior-x: contain;
		scrollbar-width: none;
		touch-action: pan-x pan-y;
		scroll-snap-type: x mandatory;
		scroll-padding-inline: var(--tab-fade);
		/* Tabs sit on the row's baseline. */
		margin-bottom: -1px;
	}
	/* The trailing chevron lives in its own slot; it never sits over a label. */
	.overflowing .st-tabs { margin-right: var(--tab-slot); }
	.tabs-more {
		position: absolute;
		right: 0;
		top: 0;
		bottom: 0;
		width: var(--tab-slot);
		display: grid;
		place-items: center;
		color: var(--st-mfg);
		pointer-events: none;
	}
	/* The fades sit at the scrolling edges, over the space a hidden tab leaves. */
	.st-tabs-wrap::before,
	.st-tabs-wrap::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		width: var(--tab-fade);
		pointer-events: none;
		opacity: 0;
		transition: opacity 0.15s;
		z-index: 1;
	}
	.st-tabs-wrap::before {
		left: 0;
		background: linear-gradient(to right, var(--st-bg), transparent);
	}
	.st-tabs-wrap::after {
		right: var(--tab-slot);
		background: linear-gradient(to left, var(--st-bg), transparent);
	}
	.st-tabs-wrap.more-l::before,
	.st-tabs-wrap.more-r::after {
		opacity: 1;
	}
	.st-tabs::-webkit-scrollbar {
		display: none;
	}
	.st-tabs.dragging {
		cursor: grabbing;
		user-select: none;
		scroll-snap-type: none;
	}
	.st-tabs.dragging button {
		pointer-events: none;
	}
	.st-tabs:focus {
		outline: none;
	}
	button {
		flex: none;
		scroll-snap-align: start;
		padding: 9px 0 10px;
		font-size: 13px;
		color: var(--st-mfg);
		border: 0;
		background: none;
		border-bottom: 2px solid transparent;
		white-space: nowrap;
		transition: opacity 0.12s;
	}
	/* A tab that does not fit whole waits fully past the edge (scroll to reach it). */
	button[data-cut] { opacity: 0; }
	button:hover {
		color: var(--st-fg);
	}
	button[aria-selected='true'] {
		color: var(--st-fg);
		border-bottom-color: var(--st-fg);
	}
	button:focus-visible {
		outline-offset: -2px;
	}
	@media (max-width: 600px) {
		button { min-height: 44px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.st-tabs-wrap::before, .st-tabs-wrap::after, button { transition: none; }
	}
	.n {
		color: var(--st-mfg);
		margin-left: 4px;
		font-size: 11px;
		font-weight: 500;
		font-variant-numeric: tabular-nums;
	}
</style>
