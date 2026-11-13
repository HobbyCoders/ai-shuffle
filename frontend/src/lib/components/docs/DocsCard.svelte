<script lang="ts">
 import { onMount } from 'svelte';
 import { layout } from '$lib/stores/layout.svelte';
 import { platform } from '$lib/stores/platform.svelte';
 import { ViewShell, ViewPanel, DetailPane, ListItem, ListGroupLabel, EmptyDetail } from '$lib/components/layout/view';
 import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
 import { extractHeadings } from '$lib/docs/markdown';
 import { DocsSearchEngine } from '$lib/docs/search';
 import DocsContent from './DocsContent.svelte';
 import DocsTableOfContents from './DocsTableOfContents.svelte';
 import DocsSearch from './DocsSearch.svelte';
 import { ChevronRight } from 'lucide-svelte';
 let { mobile = false, initialItemId }: { mobile?: boolean; initialItemId?: string | null } = $props();
 let consumedItemId: string | null = null;
 let activeCategory = $state(categories[0]?.id || 'getting-started');
 let activeSlug = $state('');
 let detailOpen = $state(false);
 let tocOpen = $state(false);
 let find = $state<{ query: string; key: number } | null>(null);
 // Never reused: DocsContent reveals each search once per key, across documents chosen in between.
 let searches = 0;
 const tocId = $props.id();
 let articleEl = $state<HTMLElement>();
 const searchEngine = new DocsSearchEngine(docs);
 const docsByCategory = Object.fromEntries(categories.map(cat => [cat.id, getDocsByCategory(cat.id)]));
 const currentDoc = $derived(getDocBySlug(activeCategory, activeSlug) || null);
 // From the Markdown itself, with the renderer's anchors: present on the first
 // paint and on every reopen of the same document (#1476).
 const headings = $derived(currentDoc ? extractHeadings(currentDoc.content) : []);
 // The tab remembers the open document, so a reload reopens it. Docs keeps no
 // browser history of its own: this is a plain read on mount, and only where
 // list and document show side by side. A phone opens on the list, as before,
 // because opening the stacked detail on load would add ViewShell's Back entry
 // on top of the one the reloaded page already stands on.
 const OPEN_KEY = 'docs.open';
 function remember(doc: string) {
  try { sessionStorage.setItem(OPEN_KEY, doc); } catch { /* Storage blocked: a reload starts over. */ }
 }
 function known(fullSlug: string): [string, string] | null {
  const [cat, slug] = fullSlug.split('/');
  return cat && slug && getDocBySlug(cat, slug) ? [cat, slug] : null;
 }
 function lastOpen(): [string, string] | null {
  try { return known(sessionStorage.getItem(OPEN_KEY) ?? ''); } catch { return null; }
 }
 onMount(() => {
  if (activeSlug || initialItemId || mobile || platform.isMobile || window.innerWidth <= 600) return;
  const [category, slug] = lastOpen() ?? [activeCategory, docsByCategory[activeCategory]?.[0]?.slug];
  if (slug) selectDoc(category, slug);
 });
 function selectDoc(category: string, slug: string, query = '') {
  activeCategory = category;
  activeSlug = slug;
  tocOpen = false;
  detailOpen = true;
  find = query ? { query, key: ++searches } : null;
  remember(`${category}/${slug}`);
  const scroll = articleEl?.closest('.view-main-slot');
  if (scroll) scroll.scrollTop = 0;
 }
 function backToList() {
  detailOpen = false;
  remember('');
 }
 function handleSearchResult(fullSlug: string, query = '') {
  const target = known(fullSlug);
  if (target) selectDoc(target[0], target[1], query);
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
<ViewShell label="Docs" {detailOpen} onBack={backToList}>
 {#snippet panel()}
  <ViewPanel title="Docs">
   {#snippet search()}<DocsSearch {searchEngine} onSelectResult={handleSearchResult} />{/snippet}
   <nav aria-label="Documentation">
    {#each categories as cat}
     {#if docsByCategory[cat.id]?.length}
      <ListGroupLabel label={cat.label} compact />
      <div class="docs-group">
      {#each docsByCategory[cat.id] as doc}
       <ListItem variant="plain" id={`${cat.id}/${doc.slug}`} name={doc.title} selected={activeCategory === cat.id && activeSlug === doc.slug} onselect={() => selectDoc(cat.id, doc.slug)} />
      {/each}
      </div>
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
        <button type="button" class="doc-inline-toggle" aria-expanded={tocOpen} aria-controls={tocId} onclick={() => tocOpen = !tocOpen}><ChevronRight size={14} strokeWidth={1.8} class="doc-inline-chev" />On this page</button>
        {#if tocOpen}<div id={tocId} class="doc-inline-list"><DocsTableOfContents {headings} showTitle={false} onSelect={() => tocOpen = false} scrollContainer={articleEl?.closest('.view-main-slot') as HTMLElement | null} /></div>{/if}
       </div>
      {/if}
      <DocsContent framed content={currentDoc.content} {find} />
     {:else}<EmptyDetail title="Choose a document" message="Browse the guides or search the docs to get started." />{/if}
    </article>
    <aside class="doc-toc"><DocsTableOfContents {headings} scrollContainer={articleEl?.closest('.view-main-slot') as HTMLElement | null} /></aside>
   </div>
  </DetailPane>
 {/snippet}
</ViewShell>
<style>
 .docs-group { margin-left: 0; }
 /* One column by default; the "On this page" rail only joins it when the
    canvas is about 1600px wide, so it does not crowd the article or read as a second nav.
    Below that the contents fold into a disclosure at the top of the article. */
 .docs-layout { display: grid; grid-template-columns: minmax(0,var(--st-prose-width)); justify-content: start; gap: var(--st-toc-gap); align-items: start; }
 .doc-content { min-width: 0; }
 .doc-toc { display: none; position: sticky; top: 0; }
 .doc-inline-toc { margin: 0 0 24px; }
 .doc-inline-toggle {
  display: inline-flex; align-items: center; gap: 6px; min-height: 28px; margin-left: -2px; padding: 0 8px 0 2px;
  border: 0; border-radius: var(--radius-control); background: none; color: var(--muted-foreground);
  font: 500 13px/1.4 var(--font-sans); cursor: pointer;
 }
 .doc-inline-toggle:hover { color: var(--foreground); }
 .doc-inline-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
 .doc-inline-toggle :global(.doc-inline-chev) { flex: none; transition: transform 150ms ease; }
 .doc-inline-toggle[aria-expanded='true'] :global(.doc-inline-chev) { transform: rotate(90deg); }
 .doc-inline-list { margin-top: 8px; max-width: 360px; }
 /* Folded into the article, the list is part of the page, not a scroller inside it. */
 .doc-inline-list :global(.docs-toc) { max-height: none; overflow: visible; }
 @media (pointer: coarse) { .doc-inline-toggle { min-height: 44px; } }
 @media (prefers-reduced-motion: reduce) { .doc-inline-toggle :global(.doc-inline-chev) { transition: none; } }
 /* A 1600px canvas measures about 1570 inside: the slot keeps a stable scrollbar gutter on both edges. */
 @container st-detail (min-width: 1560px) {
  .docs-layout { grid-template-columns: minmax(0,var(--st-prose-width)) var(--st-toc-width); }
  .doc-toc { display: block; }
  .doc-inline-toc { display: none; }
 }
</style>
