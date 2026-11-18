<script lang="ts">
 import { getContext, untrack } from 'svelte';
 import { goto } from '$app/navigation';
 import { page } from '$app/state';
 import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '$lib/components/layout/feature-panel-context';
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
 // Every selection resets the pane, even when the reader picks the same article.
 let selectionKey = $state(0);
 let selections = 0;
 const tocId = $props.id();
 let articleEl = $state<HTMLElement>();
 const searchEngine = new DocsSearchEngine(docs);
 const docsByCategory = Object.fromEntries(categories.map(cat => [cat.id, getDocsByCategory(cat.id)]));
 const currentDoc = $derived(getDocBySlug(activeCategory, activeSlug) || null);
 // From the Markdown itself, with the renderer's anchors: present on the first
 // paint and on every reopen of the same document (#1476).
 const headings = $derived(currentDoc ? extractHeadings(currentDoc.content) : []);
 // The tab remembers the open document, so a reload reopens it where list and
 // document show side by side. The phone app opens on the list, as before,
 // because opening the stacked detail on load would add ViewShell's Back entry
 // on top of the one the reloaded page already stands on. (In the web UI the
 // restore waits until Docs is on screen, and a reload on an entry Docs showed
 // an article on reopens it at any width: see below.)
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
 // Browser history, in the web UI only (#1477). Each article picked inside Docs
 // (its list, its search, a link in an article) pushes an entry of Docs' own:
 // `docsArticle`, and `docsBase`, what the entry under Docs' first step showed
 // ('' for the list). From the entry under it, only Settings' place is carried,
 // so a Settings kept open behind Docs reads its own place unchanged, as it does
 // under every other writer. Never a chat marker (Back onto it after a reload
 // would open that chat) and never an overlay's or panel's Back-stop marker
 // (its owner pops the entry carrying it). Docs never waits on another writer:
 // while such a marker is on the entry (its pop pending, or left behind), a pick
 // shows in place. Docs reads only its own keys and acts on them only while it
 // is on screen; on coming back it shows what the entry it finds names. The
 // phone app keeps ViewShell's detail entry (main).
 const ownHistory = $derived(!mobile && !platform.isMobile);
 type DocsState = App.PageState & { docsArticle?: string; docsBase?: string; viewPanelDetail?: string };
 const CARRIED = ['settingsView', 'settingsDepth', 'settingsCategory', 'settingsItem', 'settingsStaleBelow'] as const;
 const STOPS = ['viewPanelDetail', 'mobileDrawer', 'mobileControls', 'mobileControlsPage'] as const;
 const onStop = (state: Record<string, unknown>) => STOPS.some(key => state[key]);
 const entryArticle = () => (page.state as DocsState | undefined)?.docsArticle;
 const shownDoc = () => detailOpen && activeSlug ? `${activeCategory}/${activeSlug}` : '';
 const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
 const onScreen = $derived(feature?.active ?? true);
 // A reload shows what Docs last showed if that was on this very entry (an
 // article a link opened in place included); otherwise what the entry names.
 // SvelteKit numbers each entry, and the number survives a reload.
 const OPEN_ON_KEY = 'docs.openOn';
 function entryId(): number | null {
  const id = (history.state as { 'sveltekit:metadata'?: { historyIndex?: unknown } } | null)?.['sveltekit:metadata']?.historyIndex;
  return typeof id === 'number' ? id : null;
 }
 function shownHere(): [string, string] | null {
  try {
   const id = entryId();
   return id !== null && sessionStorage.getItem(OPEN_ON_KEY) === String(id) ? lastOpen() : null;
  } catch { return null; }
 }
 /** The first time Docs is on screen: what this entry showed, or names; else main's reload. */
 function restore() {
  if (activeSlug || initialItemId) return;
  const saved = shownHere() ?? known(entryArticle() ?? '');
  if (saved) return display(saved[0], saved[1]);
  if (window.innerWidth <= 600) return;
  const [category, slug] = lastOpen() ?? [activeCategory, docsByCategory[activeCategory]?.[0]?.slug];
  if (slug) selectDoc(category, slug, '', false);
 }
 /** An explicit selection: it opens at the top, and in the web UI it is a step when `record`. */
 function selectDoc(category: string, slug: string, query = '', record = true) {
  if (record && ownHistory) step(`${category}/${slug}`);
  const key = ++selections;
  selectionKey = key;
  display(category, slug, query ? { query, key } : null);
 }
 function display(category: string, slug: string, search: typeof find = null) {
  activeCategory = category;
  activeSlug = slug;
  tocOpen = false;
  detailOpen = true;
  find = search;
  remember(`${category}/${slug}`);
 }
 function step(article: string) {
  const state = (page.state ?? {}) as DocsState;
  const current = state.docsArticle;
  const shown = shownDoc();
  // The article on screen, on an entry naming it or naming none (the default a
  // wide Docs opens on): choosing it again is no step. On an entry naming
  // another article (one a link showed in place), choosing it is a step.
  if (shown === article && (current ?? article) === article) return;
  // Another owner's Back stop is this entry: its owner pops it, maybe already
  // has. A push now could be the entry that pop takes, so the pick shows in place.
  if (onStop(state)) return;
  writes++;
  const next: Record<string, unknown> = { docsArticle: article, docsBase: current === undefined ? shown : state.docsBase ?? '' };
  for (const key of CARRIED) if ((state as Record<string, unknown>)[key] !== undefined) next[key] = (state as Record<string, unknown>)[key];
  // Landed, refused or cancelled: the article shows on whatever entry is now current.
  const settle = () => { if (--writes === 0) applied = entryArticle(); };
  // persistState: a reload on this entry still names the article (main's pushState does not persist).
  goto(location.href, { shallow: true, persistState: true, state: next as App.PageState }).then(settle, settle);
 }
 // The entry Docs last showed (its `docsArticle`, undefined for a keyless one),
 // and what the keyless entry under the Docs entries last seen showed. Back,
 // Forward or a pick elsewhere while Docs is hidden change nothing on screen:
 // whatever entry Docs finds when it is shown again, it shows that. While its
 // own pushes are landing (`writes`), page.state is their echo, not a step.
 let restored = false;
 let applied: string | undefined;
 let base: string | undefined;
 let writes = 0;
 $effect(() => {
  const state = (page.state ?? {}) as DocsState;
  const on = onScreen;
  untrack(() => {
   if (!ownHistory) return;
   if (state.docsArticle !== undefined) base = state.docsBase;
   if (!on || writes) return;
   const article = state.docsArticle;
   if (!restored) { restored = true; applied = article; return restore(); }
   if (article === applied) return;
   applied = article;
   const target = known(article ?? '') ?? known(base ?? '');
   if (target) display(target[0], target[1]);
   else { activeSlug = ''; detailOpen = false; find = null; remember(''); }
  });
 });
 // What Docs shows, and on which entry, for the reload above. Only while on screen.
 $effect(() => {
  void page.state;
  const doc = shownDoc();
  if (!ownHistory || !onScreen || !restored) return;
  untrack(() => {
   const id = entryId();
   try {
    sessionStorage.setItem(OPEN_KEY, doc);
    if (id === null) sessionStorage.removeItem(OPEN_ON_KEY);
    else sessionStorage.setItem(OPEN_ON_KEY, String(id));
   } catch { /* Storage blocked: a reload starts over. */ }
  });
 });
 function backToList() {
  // On an article's own entry, Back to Docs is the browser's Back.
  if (ownHistory && detailOpen && !onStop(page.state ?? {}) && entryArticle() === shownDoc()) {
   history.back();
   return;
  }
  detailOpen = false;
  remember('');
 }
 function handleSearchResult(fullSlug: string, query = '', record = true) {
  const target = known(fullSlug);
  if (target) selectDoc(target[0], target[1], query, record);
 }
 // A deep link from elsewhere (Spotlight, Settings, a chat) shows its article on
 // the entry already there. A link inside an article is a pick (onDocLink).
 $effect(() => {
  if (!initialItemId) { consumedItemId = null; return; }
  if (initialItemId !== consumedItemId) {
   consumedItemId = initialItemId;
   handleSearchResult(initialItemId, '', false);
   layout.clearFeatureItemId();
  }
 });
</script>
<!-- The web UI records articles above; the phone app keeps ViewShell's own detail entry (main). -->
<ViewShell label="Docs" manageHistory={!ownHistory} {detailOpen} detailKey={selectionKey} onBack={backToList}>
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
      <DocsContent framed content={currentDoc.content} {find} onDocLink={(fullSlug) => handleSearchResult(fullSlug)} />
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
