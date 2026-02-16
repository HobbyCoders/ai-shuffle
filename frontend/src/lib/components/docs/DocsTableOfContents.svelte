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
			const root = scrollContainer || document.querySelector('.docs-scroll-area');
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
		top: var(--space-5);
		width: 200px;
		min-width: 200px;
		max-height: calc(100vh - 140px);
		overflow-y: auto;
		padding: var(--space-4) var(--space-3);
		border-left: 1px solid var(--border-subtle);
	}

	.toc-title {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.5px;
		margin-bottom: var(--space-3);
		padding-left: var(--space-2);
	}

	.toc-list {
		list-style: none;
		padding: 0;
		margin: 0;
	}

	.toc-item {
		margin: 0;
	}

	.toc-item.level-3 {
		padding-left: var(--space-3);
	}

	.toc-link {
		display: block;
		width: 100%;
		text-align: left;
		padding: 4px var(--space-2);
		font-size: 13px;
		color: var(--text-tertiary);
		background: none;
		border: none;
		border-left: 2px solid transparent;
		cursor: pointer;
		transition: all 0.15s;
		line-height: 1.4;
		border-radius: 0;
	}

	.toc-link:hover {
		color: var(--text-primary);
	}

	.toc-link.active {
		color: var(--primary);
		border-left-color: var(--primary);
	}

	/* Scrollbar */
	.docs-toc::-webkit-scrollbar {
		width: 4px;
	}

	.docs-toc::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 2px;
	}

	@media (max-width: 1200px) {
		.docs-toc {
			display: none;
		}
	}
</style>
