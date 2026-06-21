<script lang="ts">
	/**
	 * DocsCard — Studio-v2 documentation surface.
	 *
	 * Three-column layout (nav · prose · on-this-page TOC) centered at 1240px,
	 * framed by the shared StudioPageHeader. Theme tokens only — the Neo-Glass
	 * card-design-system overlay has been dropped in favour of the app theme.
	 */
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import { BookOpen } from 'lucide-svelte';
	import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
	import { DocsSearchEngine } from '$lib/docs/search';
	import type { DocSection } from '$lib/docs/types';
	import DocsContent from './DocsContent.svelte';
	import DocsTableOfContents from './DocsTableOfContents.svelte';
	import DocsSearch from './DocsSearch.svelte';

	interface Props {
		mobile?: boolean;
	}

	let { mobile = false }: Props = $props();

	// Active state
	let activeCategory = $state(categories[0]?.id || 'getting-started');
	let activeSlug = $state('');
	let headings = $state<DocSection[]>([]);
	let scrollEl = $state<HTMLDivElement | null>(null);

	// Search engine (lazy initialized)
	const searchEngine = new DocsSearchEngine(docs);

	// Build docs by category lookup
	const docsByCategory = $derived.by(() => {
		const map: Record<string, typeof docs> = {};
		for (const cat of categories) {
			map[cat.id] = getDocsByCategory(cat.id);
		}
		return map;
	});

	// Auto-select first doc in category when nothing is selected yet
	$effect(() => {
		const catDocs = docsByCategory[activeCategory];
		if (catDocs && catDocs.length > 0 && !activeSlug) {
			activeSlug = catDocs[0].slug;
		}
	});

	// Current document content
	const currentDoc = $derived.by(() => {
		if (!activeCategory || !activeSlug) return null;
		return getDocBySlug(activeCategory, activeSlug) || null;
	});

	function selectDoc(category: string, slug: string) {
		activeCategory = category;
		activeSlug = slug;
		headings = [];
		if (scrollEl) scrollEl.scrollTop = 0;
	}

	function handleSearchResult(fullSlug: string) {
		const [cat, slug] = fullSlug.split('/');
		if (cat && slug) selectDoc(cat, slug);
	}
</script>

<div class="dc">
	<div class="dc-scroll" bind:this={scrollEl}>
		<div class="dc-wrap">
			<header class="dc-head">
				<StudioPageHeader eyebrow="Help" title="" accent="Docs">
					{#snippet actions()}
						<DocsSearch {searchEngine} onSelectResult={handleSearchResult} />
					{/snippet}
				</StudioPageHeader>
				<p class="dc-sub">Guides for getting the most out of ai-shuffle.</p>
			</header>

			<div class="docs-layout" class:solo={mobile}>
				<!-- Left navigation -->
				{#if !mobile}
					<nav class="doc-nav">
						{#each categories as cat}
							{#if docsByCategory[cat.id]?.length}
								<div class="doc-navlabel">{cat.label}</div>
								{#each docsByCategory[cat.id] as doc}
									<button
										type="button"
										class="doc-navitem"
										class:on={activeCategory === cat.id && activeSlug === doc.slug}
										onclick={() => selectDoc(cat.id, doc.slug)}
									>
										{doc.title}
									</button>
								{/each}
							{/if}
						{/each}
					</nav>
				{/if}

				<!-- Center content -->
				<article class="doc-content">
					{#if currentDoc}
						<DocsContent
							content={currentDoc.content}
							onHeadingsExtracted={(h) => (headings = h)}
						/>
					{:else}
						<div class="doc-empty">
							<BookOpen size={44} strokeWidth={1} />
							<p>Select a document to get started.</p>
						</div>
					{/if}
				</article>

				<!-- Right table of contents -->
				{#if !mobile}
					<DocsTableOfContents {headings} scrollContainer={scrollEl} />
				{/if}
			</div>
		</div>
	</div>
</div>

<style>
	.dc {
		height: 100%;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.dc-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overflow-x: hidden;
	}
	.dc-wrap {
		max-width: 1240px;
		margin: 0 auto;
		padding: 28px 28px 64px;
	}

	.dc-head {
		margin-bottom: 24px;
	}
	.dc-sub {
		margin: 10px 0 0;
		font-size: 0.875rem;
		color: var(--muted-foreground);
	}

	/* ── 3-column grid ── */
	.docs-layout {
		display: grid;
		grid-template-columns: 214px 1fr 184px;
		gap: 28px;
		align-items: start;
	}
	.docs-layout.solo {
		grid-template-columns: 1fr;
	}

	/* ── Left nav ── */
	.doc-nav {
		position: sticky;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}
	.doc-navlabel {
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
		font-size: 10px;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--muted-foreground);
		padding: 14px 10px 5px;
		font-weight: 600;
	}
	.doc-navlabel:first-child {
		padding-top: 0;
	}
	.doc-navitem {
		padding: 7px 10px;
		border-radius: 7px;
		font-size: 13px;
		color: var(--muted-foreground);
		font-weight: 500;
		border: none;
		background: none;
		text-align: left;
		width: 100%;
		cursor: pointer;
		transition:
			background 0.12s,
			color 0.12s;
	}
	.doc-navitem:hover {
		background: var(--muted);
		color: var(--foreground);
	}
	.doc-navitem.on {
		background: var(--foreground);
		color: var(--background);
	}

	/* ── Content card ── */
	.doc-content {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 36px 42px;
		min-width: 0;
	}
	.doc-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		min-height: 320px;
		color: var(--muted-foreground);
	}
	.doc-empty p {
		font-size: 0.875rem;
	}

	/* ── Scrollbar ── */
	.dc-scroll::-webkit-scrollbar {
		width: 8px;
	}
	.dc-scroll::-webkit-scrollbar-track {
		background: transparent;
	}
	.dc-scroll::-webkit-scrollbar-thumb {
		background: var(--border);
		border-radius: 4px;
	}

	/* ── Responsive (TOC hides itself at <=1200px) ── */
	@media (max-width: 1200px) {
		.docs-layout {
			grid-template-columns: 214px 1fr;
		}
	}
	@media (max-width: 820px) {
		.docs-layout {
			grid-template-columns: 1fr;
		}
		.doc-nav {
			display: none;
		}
		.doc-content {
			padding: 26px 22px;
		}
	}
</style>
