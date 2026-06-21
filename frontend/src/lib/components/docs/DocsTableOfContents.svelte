<script lang="ts">
	/**
	 * DocsTableOfContents - Right-side sticky ToC with scroll-spy
	 *
	 * Displays h2/h3 headings from the current document and highlights
	 * the active section based on scroll position.
	 */
	import type { DocSection } from '$lib/docs/types';
	import { onMount } from 'svelte';

	interface Props {
		headings: DocSection[];
		scrollContainer?: HTMLElement | null;
	}

	let { headings, scrollContainer = null }: Props = $props();

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
			const root = scrollContainer || document.querySelector('.dc-scroll');
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

	function scrollToHeading(id: string) {
		const el = document.getElementById(id);
		if (!el) return;
		el.scrollIntoView({ behavior: 'smooth', block: 'start' });
		activeId = id;
	}
</script>

{#if headings.length > 0}
	<nav class="docs-toc">
		<div class="toc-title">On this page</div>
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
	.docs-toc {
		position: sticky;
		top: 0;
		width: 100%;
		max-height: calc(100vh - 120px);
		overflow-y: auto;
		padding: 0;
	}

	.toc-title {
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
		font-size: 10px;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.1em;
		margin-bottom: 9px;
		padding-left: 13px;
	}

	.toc-list {
		list-style: none;
		padding: 0;
		margin: 0;
	}

	.toc-item {
		margin: 0;
	}

	.toc-item.level-3 .toc-link {
		padding-left: 24px;
	}

	.toc-link {
		display: block;
		width: 100%;
		text-align: left;
		padding: 5px 0 5px 13px;
		font-size: 12px;
		color: var(--muted-foreground);
		background: none;
		border: none;
		border-left: 2px solid var(--border);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s;
		line-height: 1.4;
		border-radius: 0;
	}

	.toc-link:hover {
		color: var(--foreground);
	}

	.toc-link.active {
		color: var(--foreground);
		border-left-color: var(--primary);
		font-weight: 600;
	}

	/* Scrollbar */
	.docs-toc::-webkit-scrollbar {
		width: 4px;
	}

	.docs-toc::-webkit-scrollbar-thumb {
		background: var(--border);
		border-radius: 2px;
	}

	@media (max-width: 1200px) {
		.docs-toc {
			display: none;
		}
	}
</style>
