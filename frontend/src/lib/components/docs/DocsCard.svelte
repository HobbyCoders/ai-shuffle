<script lang="ts">
	import { layout } from '$lib/stores/layout.svelte';
	/**
	 * DocsCard — Studio-v2 documentation surface.
	 *
	 * Three-column layout (nav · prose · on-this-page TOC) centered at 1240px,
	 * framed by the shared StudioPageHeader. Theme tokens only — the Neo-Glass
	 * card-design-system overlay has been dropped in favour of the app theme.
	 */
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import { BookOpen, Menu, List, ChevronDown } from 'lucide-svelte';
	import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
	import { DocsSearchEngine } from '$lib/docs/search';
	import type { DocSection } from '$lib/docs/types';
	import DocsContent from './DocsContent.svelte';
	import DocsTableOfContents from './DocsTableOfContents.svelte';
	import DocsSearch from './DocsSearch.svelte';

	interface Props {
		mobile?: boolean;
		initialItemId?: string | null;
	}

	let { mobile = false, initialItemId }: Props = $props();
	let consumedItemId: string | null = null;

	// Active state
	let activeCategory = $state(categories[0]?.id || 'getting-started');
	let activeSlug = $state('');
	let headings = $state<DocSection[]>([]);
	let scrollEl = $state<HTMLDivElement | null>(null);

	// Mobile drop-down panel: 'nav' = doc browser, 'toc' = on-this-page, null = closed
	let mobilePanel = $state<'nav' | 'toc' | null>(null);

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
		mobilePanel = null;
		if (scrollEl) scrollEl.scrollTop = 0;
	}

	$effect(() => {
        if (!initialItemId) { consumedItemId = null; return; }
        if (initialItemId !== consumedItemId) {
            consumedItemId = initialItemId;
            handleSearchResult(initialItemId);
            layout.clearFeatureItemId();
        }
    });

	function handleSearchResult(fullSlug: string) {
		const [cat, slug] = fullSlug.split('/');
		if (cat && slug) selectDoc(cat, slug);
	}

	function togglePanel(panel: 'nav' | 'toc') {
		mobilePanel = mobilePanel === panel ? null : panel;
	}

	function scrollToHeadingMobile(id: string) {
		mobilePanel = null;
		const el = document.getElementById(id);
		if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

			<!-- Mobile nav: doc browser + on-this-page, replacing the hidden sidebars -->
			{#if mobile}
				<div class="doc-mobbar">
					<div class="doc-mobtabs">
						<button
							type="button"
							class="doc-mobtab"
							class:on={mobilePanel === 'nav'}
							onclick={() => togglePanel('nav')}
							aria-expanded={mobilePanel === 'nav'}
						>
							<Menu size={15} />
							<span class="doc-mobtab-label">{currentDoc?.title || 'Browse docs'}</span>
							<ChevronDown size={15} class="doc-mobchev" />
						</button>
						{#if headings.length > 0}
							<button
								type="button"
								class="doc-mobtab"
								class:on={mobilePanel === 'toc'}
								onclick={() => togglePanel('toc')}
								aria-expanded={mobilePanel === 'toc'}
							>
								<List size={15} />
								<span class="doc-mobtab-label">On this page</span>
								<ChevronDown size={15} class="doc-mobchev" />
							</button>
						{/if}
					</div>

					{#if mobilePanel === 'nav'}
						<nav class="doc-mobpanel">
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
					{:else if mobilePanel === 'toc'}
						<nav class="doc-mobpanel">
							<div class="doc-navlabel">On this page</div>
							{#each headings as heading}
								<button
									type="button"
									class="doc-navitem"
									class:sub={heading.level === 3}
									onclick={() => scrollToHeadingMobile(heading.id)}
								>
									{heading.title}
								</button>
							{/each}
						</nav>
					{/if}
				</div>
			{/if}

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

	/* ── Mobile nav + on-this-page (replaces the hidden sidebars) ── */
	.doc-mobbar {
		position: sticky;
		top: 0;
		z-index: 20;
		margin-bottom: 18px;
		padding-top: 4px;
		background: var(--background);
	}
	.doc-mobtabs {
		display: flex;
		gap: 8px;
	}
	.doc-mobtab {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 9px 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 9px;
		color: var(--foreground);
		font-size: 13px;
		font-weight: 500;
		cursor: pointer;
		transition:
			border-color 0.12s,
			background 0.12s;
	}
	.doc-mobtab:hover {
		border-color: var(--muted-foreground);
	}
	.doc-mobtab.on {
		border-color: var(--primary);
	}
	.doc-mobtab-label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		text-align: left;
	}
	.doc-mobtab :global(svg) {
		flex-shrink: 0;
	}
	.doc-mobtab :global(.doc-mobchev) {
		color: var(--muted-foreground);
		transition: transform 0.15s;
	}
	.doc-mobtab.on :global(.doc-mobchev) {
		transform: rotate(180deg);
	}

	.doc-mobpanel {
		position: absolute;
		top: calc(100% + 6px);
		left: 0;
		right: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
		max-height: min(62vh, 460px);
		overflow-y: auto;
		padding: 8px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 11px;
		box-shadow: 0 16px 40px rgba(0, 0, 0, 0.32);
	}
	.doc-navitem.sub {
		padding-left: 22px;
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
	@media (max-width: 639px) {
		.dc-wrap {
			padding: 18px 16px 48px;
		}
		.doc-content {
			padding: 22px 16px;
		}
	}
</style>
