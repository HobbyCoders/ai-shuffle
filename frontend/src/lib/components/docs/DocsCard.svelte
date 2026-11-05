<script lang="ts">
 import { onMount } from 'svelte';
 import { layout } from '$lib/stores/layout.svelte';
 import { platform } from '$lib/stores/platform.svelte';
 import { ViewShell, ViewPanel, DetailPane, ListItem, ListGroupLabel, EmptyDetail } from '$lib/components/layout/view';
 import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
 import { DocsSearchEngine } from '$lib/docs/search';
 import type { DocSection } from '$lib/docs/types';
 import DocsContent from './DocsContent.svelte';
 import DocsTableOfContents from './DocsTableOfContents.svelte';
 import DocsSearch from './DocsSearch.svelte';
 let { mobile = false, initialItemId }: { mobile?: boolean; initialItemId?: string | null } = $props();
 let consumedItemId: string | null = null;
 let activeCategory = $state(categories[0]?.id || 'getting-started');
 let activeSlug = $state('');
 let detailOpen = $state(false);
 let headings = $state<DocSection[]>([]);
 let tocOpen = $state(false);
 const tocId = $props.id();
 let articleEl = $state<HTMLElement>();
 const searchEngine = new DocsSearchEngine(docs);
 const docsByCategory = Object.fromEntries(categories.map(cat => [cat.id, getDocsByCategory(cat.id)]));
 const currentDoc = $derived(getDocBySlug(activeCategory, activeSlug) || null);
 onMount(() => {
  if (!activeSlug && !mobile && !platform.isMobile && window.innerWidth > 600) {
   const first = docsByCategory[activeCategory]?.[0];
   if (first) selectDoc(activeCategory, first.slug);
  }
 });
 function selectDoc(category: string, slug: string) {
  activeCategory = category;
  activeSlug = slug;
  headings = [];
  tocOpen = false;
  detailOpen = true;
  const scroll = articleEl?.closest('.view-main-slot');
  if (scroll) scroll.scrollTop = 0;
 }
 function handleSearchResult(fullSlug: string) {
  const [cat, slug] = fullSlug.split('/');
  if (cat && slug) selectDoc(cat, slug);
 }
 $effect(() => {
  if (!initialItemId) { consumedItemId = null; return; }
  if (initialItemId !== consumedItemId) {
   consumedItemId = initialItemId;
   handleSearchResult(initialItemId);
   layout.clearFeatureItemId();
  }
 });
</script>
<ViewShell label="Docs" {detailOpen} onBack={() => detailOpen = false}>
 {#snippet panel()}
  <ViewPanel title="Docs">
   {#snippet search()}<DocsSearch {searchEngine} onSelectResult={handleSearchResult} />{/snippet}
   <nav aria-label="Documentation">
    {#each categories as cat}
     {#if docsByCategory[cat.id]?.length}
      <ListGroupLabel label={cat.label} compact />
      {#each docsByCategory[cat.id] as doc}
       <ListItem variant="plain" id={`${cat.id}/${doc.slug}`} name={doc.title} selected={activeCategory === cat.id && activeSlug === doc.slug} onselect={() => selectDoc(cat.id, doc.slug)} />
      {/each}
     {/if}
    {/each}
   </nav>
  </ViewPanel>
 {/snippet}
 {#snippet main(back)}
  <DetailPane width="docs" name={currentDoc?.title || "Docs"} backLabel="Docs" onBack={back}>
   <div class="docs-layout">
    <article class="doc-content" bind:this={articleEl}>
     {#if currentDoc}
      {#if headings.length}
       <div class="doc-inline-toc">
        <button type="button" class="st-btn" aria-expanded={tocOpen} aria-controls={tocId} onclick={() => tocOpen = !tocOpen}>On this page</button>
        {#if tocOpen}<div id={tocId}><DocsTableOfContents {headings} showTitle={false} onSelect={() => tocOpen = false} scrollContainer={articleEl?.closest('.view-main-slot') as HTMLElement | null} /></div>{/if}
       </div>
      {/if}
      <DocsContent framed content={currentDoc.content} onHeadingsExtracted={h => headings = h} />
     {:else}<EmptyDetail title="Choose a document" message="Browse the guides or search the docs to get started." />{/if}
    </article>
    <aside class="doc-toc"><DocsTableOfContents {headings} scrollContainer={articleEl?.closest('.view-main-slot') as HTMLElement | null} /></aside>
   </div>
  </DetailPane>
 {/snippet}
</ViewShell>
<style>
 .docs-layout { display: grid; grid-template-columns: minmax(0,var(--st-prose-width)) var(--st-toc-width); justify-content: start; gap: var(--st-toc-gap); align-items: start; }
 .doc-content { min-width: 0; }
 .doc-toc { position: sticky; top: 0; }
 .doc-inline-toc { display: none; margin-bottom: 24px; }
 @container settings (max-width: 1000px) { .docs-layout { grid-template-columns: minmax(0,var(--st-prose-width)); } .doc-toc { display: none; } .doc-inline-toc { display: block; } }
</style>
