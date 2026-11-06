<script lang="ts">
	/**
	 * DocsTableOfContents - Right-side sticky ToC with scroll-spy
	 *
	 * Displays h2/h3 headings from the current document and highlights
	 * the active section based on scroll position.
	 */
	import type { DocSection } from '$lib/docs/types';
	import { onMount, tick } from 'svelte';

	interface Props {
		headings: DocSection[];
		scrollContainer?: HTMLElement | null;
		showTitle?: boolean;
		onSelect?: () => void;
	}

	let { headings, scrollContainer = null, showTitle = true, onSelect }: Props = $props();

	let activeId = $state('');

	// Scroll-spy via IntersectionObserver
	let observer: IntersectionObserver | null = null;

	onMount(() => {
		return () => {
			observer?.disconnect();
		};
	});

	$effect(() => {
		observer?.disconnect();

		if (headings.length === 0) return;

		// Small delay to let DOM update
		const timer = setTimeout(() => {
			const root = scrollContainer;
			if (!root) return;

			observer = new IntersectionObserver(
				(entries) => {
					for (const entry of entries) {
						if (entry.isIntersecting) {
							activeId = entry.target.id;
						}
					}
				},
				{
					root: root,
					rootMargin: '-80px 0px -70% 0px',
					threshold: 0
				}
			);

			for (const heading of headings) {
				const el = document.getElementById(heading.id);
				if (el) observer.observe(el);
			}
		}, 100);

		return () => {
			clearTimeout(timer);
			observer?.disconnect();
		};
	});

	async function scrollToHeading(id: string) {
		onSelect?.();
		await tick();
		const el = document.getElementById(id);
		if (!el) return;
		el.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
		activeId = id;
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
 .toc-title { font-size:12px; font-weight:500; color:var(--muted-foreground); margin-bottom:12px; }
 .toc-list { list-style:none; padding:0; margin:0; }
 .toc-item.level-3 { padding-left:12px; }
 .toc-link { display:block; width:100%; text-align:left; padding:7px 8px; font:12px/1.5 var(--font-sans); color:var(--muted-foreground); border:0; border-radius:var(--radius-control); background:none; cursor:pointer; }
 .toc-link:hover { color:var(--foreground); background:var(--hover-overlay); }
 .toc-link.active { color:var(--foreground); background:var(--row-selected); }
 @media (pointer:coarse) { .toc-link { min-height:44px; } }
</style>
