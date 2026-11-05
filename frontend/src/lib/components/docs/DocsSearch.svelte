<script lang="ts">
 import { PanelSearch, ListItem } from '$lib/components/layout/view';
 import type { DocsSearchEngine } from '$lib/docs/search';
 let { searchEngine, onSelectResult }: { searchEngine: DocsSearchEngine; onSelectResult: (slug:string) => void } = $props();
 let query = $state('');
 let selectedIndex = $state(-1);
 const results = $derived(query.trim().length >= 2 ? searchEngine.search(query) : []);
 function select(slug: string) { onSelectResult(slug); query = ''; selectedIndex = -1; }
 function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') { e.preventDefault(); selectedIndex = Math.min(selectedIndex + 1,results.length - 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); selectedIndex = Math.max(selectedIndex - 1,-1); }
  else if (e.key === 'Enter' && results[selectedIndex]) { e.preventDefault(); select(results[selectedIndex].slug); }
  else if (e.key === 'Escape') { query = ''; selectedIndex = -1; }
 }
</script>
<PanelSearch clearable bind:value={query} placeholder="Search docs..." label="Search docs" onkeydown={handleKeydown} oninput={() => selectedIndex = -1} />
{#if query.trim().length >= 2}
 <div class="search-results" aria-label="Doc search results">
  {#each results as result,i}
   <ListItem variant="plain" id={result.slug} name={result.title} summary={result.snippets[0]} selected={i === selectedIndex} onselect={() => select(result.slug)} />
  {:else}<p role="status">No docs match “{query}”. Try another search.</p>{/each}
 </div>
{/if}
<style>
 .search-results { max-height: 360px; overflow: auto; padding-top: 8px; }
 p { font-size: 13px; color: var(--st-mfg); padding: 12px; }
</style>
