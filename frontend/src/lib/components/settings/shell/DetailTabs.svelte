<script lang="ts" module>
	export interface DetailTab {
		id: string;
		label?: string;
		count?: number | string;
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
</script>

<script lang="ts">
	/**
	 * DetailTabs: tabs of one detail (role=tablist). Arrow keys, Home and End
	 * move and select. DetailPane renders it only when there are 2+ tabs.
	 *
	 * When the tabs do not fit, the strip scrolls sideways: touch and trackpad
	 * natively, a vertical mouse wheel turns into sideways scroll, a mouse can
	 * drag it, and the edge with more tabs fades. The selected tab is always
	 * kept in view (after selection, keyboard moves and resizes).
	 */
	import { tick } from 'svelte';

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
	let moreLeft = $state(false);
	let moreRight = $state(false);
	let dragging = $state(false);
	let drag: { x: number; left: number; moved: boolean; id: number } | null = null;
	/** A drag ends with a click on the tab under the pointer; swallow it. */
	let dragEndedAt = 0;

	function updateFades() {
		if (!list) return;
		const max = list.scrollWidth - list.clientWidth;
		moreLeft = list.scrollLeft > 2;
		moreRight = max > 2 && list.scrollLeft < max - 2;
	}

	function reveal(id: string | null) {
		if (!list || !id) return;
		const el = Array.from(list.querySelectorAll<HTMLElement>('[data-tab-id]')).find((t) => t.dataset.tabId === id);
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

	// ...and when the strip changes size.
	$effect(() => {
		if (!list || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => reveal(current));
		ro.observe(list);
		return () => ro.disconnect();
	});

	function onkeydown(event: KeyboardEvent) {
		const i = tabs.findIndex((t) => t.id === current);
		let next = -1;
		if (event.key === 'ArrowRight') next = (i + 1) % tabs.length;
		else if (event.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = tabs.length - 1;
		if (next < 0) return;
		event.preventDefault();
		onselect(tabs[next].id);
		list?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus({ preventScroll: true });
		reveal(tabs[next].id);
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

<div class="st-tabs-wrap" class:more-l={moreLeft} class:more-r={moreRight}>
	<div
		class="st-tabs"
		class:dragging
		role="tablist"
		aria-label={label}
		bind:this={list}
		tabindex="-1"
		{onkeydown}
		onscroll={updateFades}
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
				aria-selected={tab.id === current}
				aria-controls="{idPrefix}-panel"
				tabindex={tab.id === current ? 0 : -1}
				onclick={() => choose(tab.id)}
			>
				{tab.label ?? tab.id}{#if tab.count !== undefined && tab.count !== ''}<span class="n">{tab.count}</span>{/if}
			</button>
		{/each}
	</div>
</div>

<style>
	.st-tabs-wrap {
		position: relative;
		margin-top: 14px;
	}
	.st-tabs-wrap::before,
	.st-tabs-wrap::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		width: 36px;
		pointer-events: none;
		opacity: 0;
		transition: opacity 0.15s;
		z-index: 1;
	}
	.st-tabs-wrap::before {
		left: 0;
		background: linear-gradient(to right, var(--st-bg) 15%, transparent);
	}
	.st-tabs-wrap::after {
		right: 0;
		background: linear-gradient(to left, var(--st-bg) 15%, transparent);
	}
	.st-tabs-wrap.more-l::before,
	.st-tabs-wrap.more-r::after {
		opacity: 1;
	}
	.st-tabs {
		display: flex;
		gap: 20px;
		overflow-x: auto;
		overscroll-behavior-x: contain;
		scrollbar-width: none;
		touch-action: pan-x pan-y;
		/* The baseline stays put while the tabs scroll over it. */
		background: linear-gradient(var(--st-border), var(--st-border)) left bottom / 100% 1px no-repeat;
	}
	.st-tabs::-webkit-scrollbar {
		display: none;
	}
	.st-tabs.dragging {
		cursor: grabbing;
		user-select: none;
	}
	.st-tabs.dragging button {
		pointer-events: none;
	}
	.st-tabs:focus {
		outline: none;
	}
	button {
		flex: none;
		padding: 9px 0 10px;
		font-size: 13.5px;
		color: var(--st-mfg);
		border: 0;
		background: none;
		border-bottom: 2px solid transparent;
		white-space: nowrap;
	}
	button:hover {
		color: var(--st-fg);
	}
	button[aria-selected='true'] {
		color: var(--st-fg);
		border-bottom-color: var(--st-fg);
		font-weight: 500;
	}
	button:focus-visible {
		outline-offset: -2px;
	}
	.n {
		font-size: 11.5px;
		color: var(--st-subtle);
		margin-left: 5px;
		font-variant-numeric: tabular-nums;
	}
</style>
