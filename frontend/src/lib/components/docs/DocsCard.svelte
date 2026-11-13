<script lang="ts">
 import { getContext, untrack } from 'svelte';
 import { page } from '$app/state';
 import { pushState, replaceState } from '$app/navigation';
 import { layout } from '$lib/stores/layout.svelte';
 import { platform } from '$lib/stores/platform.svelte';
 import { ViewShell, ViewPanel, DetailPane, ListItem, ListGroupLabel, EmptyDetail } from '$lib/components/layout/view';
 import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '$lib/components/layout/feature-panel-context';
 import { categories, docs, getDocsByCategory, getDocBySlug } from '$lib/docs/index';
 import { ENTRY_KEY, currentEntryId, keptEntry, type DocsEntry } from '$lib/docs/history';
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
 let stacked = $state(false);
 let tocOpen = $state(false);
 let find = $state<{ query: string; key: number } | null>(null);
 // Never reused: DocsContent reveals each search once per key, across documents chosen in between.
 let searches = 0;
 const tocId = $props.id();
 let articleEl = $state<HTMLElement>();
 const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
 const active = $derived(feature?.active ?? true);
 const searchEngine = new DocsSearchEngine(docs);
 const docsByCategory = Object.fromEntries(categories.map(cat => [cat.id, getDocsByCategory(cat.id)]));
 const currentDoc = $derived(detailOpen ? getDocBySlug(activeCategory, activeSlug) || null : null);
 // From the Markdown itself, with the renderer's anchors: present on the first
 // paint and on every reopen of the same document (#1476).
 const headings = $derived(currentDoc ? extractHeadings(currentDoc.content) : []);
 /** What Docs shows: a document, or '' for the list. */
 const shown = $derived(currentDoc ? `${activeCategory}/${activeSlug}` : '');

 /**
  * Browser history (#1477, see lib/docs/history.ts). The entry Docs stands on
  * always says what it shows. A document chosen in place of what that entry
  * shows (another document, or the list) is a new entry; opening Docs or a
  * deep link relabels the entry it opens on, so Docs adds no step nobody asked
  * for. Back and Forward onto a Docs entry show what it says, at any width, and
  * reopen Docs from another destination (AppPage). The tab remembers what is
  * open and the entry it is on, so a reload restores that entry instead of
  * adding one.
  */
 const OPEN_KEY = 'docs.open';
 function save(key: string, value: string) {
  try { sessionStorage.setItem(key, value); } catch { /* Storage blocked: a reload starts over. */ }
 }
 function load(key: string): string {
  try { return sessionStorage.getItem(key) ?? ''; } catch { return ''; }
 }
 function entry(): DocsEntry {
  return (page.state ?? {}) as DocsEntry;
 }
 /** Remember what is open and, when Docs stands on it, which entry says so. */
 function note() {
  save(OPEN_KEY, shown);
  seen = entry().docsDoc;
  const id = currentEntryId();
  if (id !== null && seen === shown) save(ENTRY_KEY, JSON.stringify({ id, doc: shown, step: !!entry().docsStep }));
 }
 function known(slug: string): [string, string] | null {
  const [cat, name] = slug.split('/');
  return cat && name && getDocBySlug(cat, name) ? [cat, name] : null;
 }
 function show(category: string, slug: string, query = '') {
  activeCategory = category;
  activeSlug = slug;
  tocOpen = false;
  detailOpen = true;
  find = query ? { query, key: ++searches } : null;
  const scroll = articleEl?.closest('.view-main-slot');
  if (scroll) scroll.scrollTop = 0;
 }
 /** Show what an entry says: a known document, else the list. */
 function showEntry(doc: string) {
  const target = known(doc);
  if (!target) detailOpen = false;
  else if (target.join('/') !== shown) show(...target);
 }
 /** Make the current entry say what Docs shows; `step` when Docs pushed it. */
 function label(step = !!entry().docsStep) {
  try { replaceState('', { ...page.state, docsDoc: shown, docsStep: step } as App.PageState); } catch { /* Routerless tests. */ }
  note();
 }
 /** A document the reader chose: a list row, a search result or a link in a document. */
 function selectDoc(category: string, slug: string, query = '') {
  if (`${category}/${slug}` === shown) { show(category, slug, query); return; } // A search inside it adds no step.
  // A new step in place of what this entry showed (a document, or the list);
  // a first document when Docs stood on no entry of its own relabels it.
  const step = active && entry().docsDoc === shown;
  show(category, slug, query);
  if (!step) { label(false); return; }
  try { pushState('', { ...page.state, docsDoc: shown, docsStep: true } as App.PageState); } catch { /* Routerless tests. */ }
  note();
 }
 /** First paint: the entry Docs opened on, else the entry a reload kept, else the last document. */
 function start(doc: string | undefined) {
  if (activeSlug || initialItemId) return;
  if (doc !== undefined) { showEntry(doc); if (entry().docsDoc === shown) note(); else label(); return; }
  const kept = keptEntry();
  if (kept) {
   // A reload: SvelteKit emptied this entry's state; it was ours, so it says the same again.
   showEntry(kept.docsDoc);
   label(kept.docsStep);
   return;
  }
  const last = known(load(OPEN_KEY)); // A plain slug, also from before entries were kept.
  if (last) show(...last);
  else if (!mobile && !platform.isMobile && window.innerWidth > 600) {
   const first = docsByCategory[activeCategory]?.[0];
   if (first) show(activeCategory, first.slug);
  }
  label(false);
 }
 let started = false;
 let seen: string | undefined;
 let wasOn = false;
 $effect(() => {
  const doc = entry().docsDoc;
  const on = active;
  untrack(() => {
   const stepped = doc !== seen; // Back or Forward, maybe while another destination was shown.
   seen = doc;
   const opened = on && !wasOn;
   wasOn = on;
   if (!on || (!stepped && !opened)) return;
   if (!started) { started = true; start(doc); return; }
   // Docs opened on an entry that names no document: that entry now stands for it.
   if (doc === undefined) { if (opened) label(false); return; }
   showEntry(doc);
   if (entry().docsDoc === shown) note(); else label();
  });
 });
 /** The detail's own Back on a phone: one step down when Docs pushed this entry, else the list. */
 function backToList() {
  if (entry().docsStep && entry().docsDoc === shown) { history.back(); return; }
  detailOpen = false;
  label();
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
<ViewShell label="Docs" {detailOpen} bind:stacked manageHistory={false} onBack={backToList}>
 {#snippet panel()}
  <ViewPanel title="Docs">
   {#snippet search()}<DocsSearch {searchEngine} onSelectResult={handleSearchResult} />{/snippet}
   <nav aria-label="Documentation">
    {#each categories as cat}
     {#if docsByCategory[cat.id]?.length}
      <ListGroupLabel label={cat.label} compact />
      <div class="docs-group">
      {#each docsByCategory[cat.id] as doc}
       <ListItem variant="plain" id={`${cat.id}/${doc.slug}`} name={doc.title} selected={shown === `${cat.id}/${doc.slug}`} onselect={() => selectDoc(cat.id, doc.slug)} />
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
