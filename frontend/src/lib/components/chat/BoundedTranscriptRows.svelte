<script lang="ts" generics="T">
	import { onMount, tick, untrack, type Snippet } from 'svelte';
	import { createTranscriptWindow, type TranscriptRange } from './transcript-window';

	/** Bounded nested rows: a single huge work log must not bypass the outer cap. */
	let { items, keyOf, children, revealKey = null, startAtTail = false, pagingEnabled = true, label = 'log entries' }: {
		items: readonly T[];
		keyOf: (item: T) => string;
		children: Snippet<[T]>;
		revealKey?: string | null;
		startAtTail?: boolean;
		/** Parent disables automatic edge paging while following the live tail. */
		pagingEnabled?: boolean;
		label?: string;
	} = $props();
	const window = createTranscriptWindow({ cap: 60, step: 20 });
	let range = $state<TranscriptRange>({ start: 0, end: 0 });
	let root = $state<HTMLDivElement>();
	let scroller: HTMLElement | null = null;
	let browsing = false;
	let shifting = false;
	let version = 0;
	let previousTop = 0;
	const visible = $derived(items.slice(range.start, range.end));

	function apply(next: TranscriptRange, preserve = true) {
		if (next.start === range.start && next.end === range.end) return;
		const top = scroller?.getBoundingClientRect().top ?? 0;
		const candidates = preserve && root
			? Array.from(root.querySelectorAll<HTMLElement>(':scope > [data-slice-key]'))
				.filter(node => {
					const index = Number(node.dataset.sliceIndex);
					return index >= next.start && index < next.end;
				}).map(node => node.firstElementChild as HTMLElement).filter(Boolean)
			: [];
		const anchor = candidates.find(node => node.getBoundingClientRect().bottom > top) ?? candidates.at(-1);
		const before = anchor?.getBoundingClientRect().top;
		const shift = ++version;
		shifting = true;
		range = next;
		void tick().then(() => {
			if (shift !== version) return;
			if (scroller && anchor?.isConnected && before !== undefined) {
				scroller.scrollTop += anchor.getBoundingClientRect().top - before;
			}
			previousTop = scroller?.scrollTop ?? 0;
			shifting = false;
		});
	}
	function older() { browsing = true; apply(window.loadOlder()); }
	function newer() { browsing = true; apply(window.extendNewer()); }
	$effect.pre(() => {
		const count = items.length;
		const tail = startAtTail;
		untrack(() => apply(window.setItemCount(count, { followTail: tail && !browsing })));
	});
	let lastReveal: string | null = null;
	$effect.pre(() => {
		const target = revealKey;
		if (!target) { lastReveal = null; return; }
		if (target === lastReveal) return;
		const index = items.findIndex(item => keyOf(item) === target);
		if (index >= 0) untrack(() => {
			lastReveal = target;
			browsing = true;
			if (index < range.start || index >= range.end) apply(window.windowAround(index), false);
		});
	});
	onMount(() => {
		for (let node = root?.parentElement; node; node = node.parentElement) {
			if (['auto', 'scroll'].includes(getComputedStyle(node).overflowY)) { scroller = node; break; }
		}
		previousTop = scroller?.scrollTop ?? 0;
		let frame = 0;
		function scroll() {
			if (items.length <= window.cap || frame || shifting || document.hidden || !scroller) return;
			frame = requestAnimationFrame(() => {
				frame = 0;
				if (!scroller || !root || shifting) return;
				const current = scroller.scrollTop;
				if (!pagingEnabled) { previousTop = current; return; }
				const box = scroller.getBoundingClientRect();
				const first = root.querySelector<HTMLElement>('[data-slice-key] > :first-child');
				const last = root.querySelector<HTMLElement>('[data-slice-key]:last-of-type > :last-child');
				if (current < previousTop && range.start > 0 && first && first.getBoundingClientRect().top > box.top - 160) older();
				else if (current > previousTop && range.end < items.length && last && last.getBoundingClientRect().bottom < box.bottom + 160) newer();
				previousTop = current;
			});
		}
		function visibility() { apply(window.pauseWhenHidden(document.hidden), false); }
		visibility();
		scroller?.addEventListener('scroll', scroll, { passive: true });
		document.addEventListener('visibilitychange', visibility);
		return () => {
			version++;
			cancelAnimationFrame(frame);
			scroller?.removeEventListener('scroll', scroll);
			document.removeEventListener('visibilitychange', visibility);
		};
	});
</script>

<div class="bounded-rows" bind:this={root}>
	{#if range.start > 0}
		<button type="button" class="slice-page" onclick={older}>Show earlier {label}</button>
	{/if}
	{#each visible as item, index (keyOf(item))}
		<div class="slice-entry" data-slice-key={keyOf(item)} data-slice-index={range.start + index}>
			{@render children(item)}
		</div>
	{/each}
	{#if range.end < items.length}
		<button type="button" class="slice-page" onclick={newer}>Show newer {label}</button>
	{/if}
</div>

<style>
	.bounded-rows, .slice-entry { display: contents; }
	.slice-page {
		min-height: 44px; padding: 8px 12px; margin-block: 8px;
		border: 1px solid var(--border); border-radius: 8px;
		background: var(--background); color: var(--muted-foreground);
		font: inherit; font-size: 12px; cursor: pointer;
		transition: color 200ms ease, background 200ms ease;
	}
	.slice-page:hover { background: var(--muted); color: var(--foreground); }
	.slice-page:active { background: var(--accent); }
	.slice-page:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	@media (prefers-reduced-motion: reduce) { .slice-page { transition: none; } }
</style>
