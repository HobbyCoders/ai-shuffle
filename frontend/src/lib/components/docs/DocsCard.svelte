<script lang="ts">
	/**
	 * DocsCard - Main documentation feature page
	 *
	 * Three-column layout: sidebar nav | content | table of contents
	 * Uses FeaturePageLayout with category tabs and search.
	 */
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import '../deck/cards/card-design-system.css';
	import { BookOpen } from 'lucide-svelte';
	import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
	import { DocsSearchEngine } from '$lib/docs/search';
	import type { DocSection } from '$lib/docs/types';
	import DocsSidebar from './DocsSidebar.svelte';
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
	let scrollAreaEl = $state<HTMLDivElement | null>(null);

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

	// Auto-select first doc in category when category changes
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
		// Scroll content to top
		if (scrollAreaEl) scrollAreaEl.scrollTop = 0;
	}

	function handleSearchResult(fullSlug: string) {
		const [cat, slug] = fullSlug.split('/');
		if (cat && slug) {
			selectDoc(cat, slug);
		}
	}

	function handleTabChange(catId: string) {
		activeCategory = catId;
		const catDocs = docsByCategory[catId];
		activeSlug = catDocs?.[0]?.slug || '';
		headings = [];
		if (scrollAreaEl) scrollAreaEl.scrollTop = 0;
	}
</script>

<FeaturePageLayout
	icon={BookOpen}
	title="Documentation"
	itemCount={docs.length}
	itemLabel="pages"
	noPadding={true}
>
	{#snippet headerActions()}
		<DocsSearch {searchEngine} onSelectResult={handleSearchResult} />
	{/snippet}

	{#snippet headerTabs()}
		<div class="fp-tab-bar">
			{#each categories as cat}
				<button
					class="fp-tab"
					class:active={activeCategory === cat.id}
					onclick={() => handleTabChange(cat.id)}
				>
					{cat.label}
				</button>
			{/each}
		</div>
	{/snippet}

	{#snippet children()}
		<div class="docs-layout">
			<!-- Left sidebar navigation -->
			{#if !mobile}
				<DocsSidebar
					{categories}
					{docsByCategory}
					{activeSlug}
					{activeCategory}
					onSelectDoc={selectDoc}
				/>
			{/if}

			<!-- Center content area -->
			<div class="docs-scroll-area" bind:this={scrollAreaEl}>
				{#if currentDoc}
					<DocsContent
						content={currentDoc.content}
						onHeadingsExtracted={(h) => (headings = h)}
					/>
				{:else}
					<div class="docs-empty">
						<BookOpen size={48} strokeWidth={1} />
						<p>Select a document from the sidebar to get started.</p>
					</div>
				{/if}
			</div>

			<!-- Right table of contents -->
			{#if !mobile}
				<DocsTableOfContents {headings} scrollContainer={scrollAreaEl} />
			{/if}
		</div>
	{/snippet}
</FeaturePageLayout>

<style>
	.docs-layout {
		display: flex;
		height: 100%;
		overflow: hidden;
	}

	.docs-scroll-area {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		display: flex;
		justify-content: center;
	}

	.docs-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-3);
		height: 100%;
		min-height: 400px;
		color: var(--text-tertiary);
	}

	.docs-empty p {
		font-size: 14px;
	}

	/* Tab bar styling */
	.fp-tab-bar {
		display: flex;
		gap: 2px;
		padding: 0 var(--space-3);
		overflow-x: auto;
	}

	.fp-tab {
		padding: 8px 16px;
		font-size: 13px;
		font-weight: 500;
		color: var(--text-tertiary);
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		cursor: pointer;
		white-space: nowrap;
		transition: all 0.15s;
	}

	.fp-tab:hover {
		color: var(--text-primary);
	}

	.fp-tab.active {
		color: var(--primary);
		border-bottom-color: var(--primary);
	}

	/* Scrollbar */
	.docs-scroll-area::-webkit-scrollbar {
		width: 6px;
	}

	.docs-scroll-area::-webkit-scrollbar-track {
		background: transparent;
	}

	.docs-scroll-area::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 3px;
	}

	@media (max-width: 768px) {
		.docs-layout {
			flex-direction: column;
		}
	}
</style>
