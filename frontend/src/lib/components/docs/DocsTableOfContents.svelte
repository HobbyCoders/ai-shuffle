<script lang="ts">
	/**
	 * DocsTableOfContents - Right-side sticky ToC with scroll-spy
	 *
	 * Displays h2/h3 headings from the current document and highlights
	 * the active section based on scroll position.
	 */
	import type { DocSection } from '$lib/docs/types';
	import { tick } from 'svelte';

	interface Props {
		headings: DocSection[];
		scrollContainer?: HTMLElement | null;
		showTitle?: boolean;
		onSelect?: () => void;
	}

	let { headings, scrollContainer = null, showTitle = true, onSelect }: Props = $props();

	let activeId = $state('');
	let chosenUntil = 0;

	/** Where a heading counts as reached: the top 30% of the reading pane. */
	const REACHED_FRACTION = 0.3;

	/**
	 * The heading the reader is in, from where the headings are now. It holds
	 * after a jump (End, dragging the scrollbar), which skips past a heading
	 * without it ever passing a marker.
	 */
	function headingAtScroll(root: HTMLElement): string {
		// The last heading can never reach the line on a page that ends right under it.
		if (root.scrollHeight - root.scrollTop - root.clientHeight < 8) return headings[headings.length - 1].id;
		const line = root.getBoundingClientRect().top + root.clientHeight * REACHED_FRACTION;
		let current = '';
		for (const heading of headings) {
			const el = document.getElementById(heading.id);
			if (!el) continue;
			if (el.getBoundingClientRect().top > line) break;
			current = heading.id;
		}
		return current;
	}

	$effect(() => {
		if (headings.length === 0) return;
		let removeScroll: (() => void) | undefined;
		let resize: ResizeObserver | undefined;

		// Small delay to let DOM update
		const timer = setTimeout(() => {
			const root = scrollContainer;
			if (!root) return;

			// How far the pane's top is past the current heading. Measured before
			// a reflow, because the resize callback only runs after one.
			let pastHeading = 0;
			const headingTop = () => {
				const el = activeId ? document.getElementById(activeId) : null;
				return el ? el.getBoundingClientRect().top - root.getBoundingClientRect().top : null;
			};
			const onScroll = () => {
				if (performance.now() >= chosenUntil) activeId = headingAtScroll(root);
				pastHeading = -(headingTop() ?? 0);
			};
			root.addEventListener('scroll', onScroll, { passive: true });
			removeScroll = () => root.removeEventListener('scroll', onScroll);
			onScroll(); // The reader may already be part way down when this list starts.

			// A new width reflows the text: keep the reader the same distance past
			// the current heading, rather than back at its start.
			let width = root.clientWidth;
			resize = new ResizeObserver(() => {
				if (root.clientWidth === width) return;
				width = root.clientWidth;
				const top = headingTop();
				if (top !== null && root.scrollTop > 0) root.scrollTop += top + pastHeading;
			});
			resize.observe(root);
		}, 100);

		return () => {
			clearTimeout(timer);
			resize?.disconnect();
			removeScroll?.();
		};
	});

	async function scrollToHeading(id: string) {
		onSelect?.();
		await tick();
		const el = document.getElementById(id);
		if (!el) return;
		el.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
		activeId = id;
		chosenUntil = performance.now() + 1000; // A chosen heading outlasts its own smooth scroll.
	}
</script>

{#if headings.length > 0}
	<nav class="docs-toc" aria-label="On this page">
		{#if showTitle}<div class="toc-title">On this page</div>{/if}
		<ul class="toc-list">
			{#each headings as heading}
				<li class="toc-item" class:level-3={heading.level === 3}>
					<button
						class="toc-link"
						class:active={activeId === heading.id}
						onclick={() => scrollToHeading(heading.id)}
					>
						{heading.title}
					</button>
				</li>
			{/each}
		</ul>
	</nav>
{/if}

<style>
 .docs-toc { width:100%; max-height:calc(100vh - 120px); overflow:auto; }
 .toc-title { font-size:13px; font-weight:500; color:var(--muted-foreground); margin-bottom:12px; }
 .toc-list { list-style:none; padding:0; margin:0; }
 /* A thin guide line, with the current section marked by a 2px bar and the
    primary text colour: never a filled pill that reads as a second nav selection. */
 .toc-list { border-left: 1px solid var(--border); }
 .toc-item.level-3 .toc-link { padding-left:24px; }
 /* 13px rows, 16px apart (8px of padding each side of a 19.5px line). */
 .toc-link { display:block; width:calc(100% + 1px); margin-left:-1px; text-align:left; padding:8px 8px 8px 12px; font:13px/1.5 var(--font-sans); color:var(--muted-foreground); border:0; border-left:2px solid transparent; border-radius:0; background:none; cursor:pointer; }
 .toc-link:hover { color:var(--foreground); }
 .toc-link:focus-visible { outline:2px solid var(--ring); outline-offset:-2px; }
 .toc-link.active { color:var(--foreground); border-left-color:var(--foreground); }
 @media (pointer:coarse) { .toc-link { min-height:44px; } }
</style>
