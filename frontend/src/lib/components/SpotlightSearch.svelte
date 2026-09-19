<script lang="ts">
 import { tick } from 'svelte';
 import { Search, ArrowUpRight } from 'lucide-svelte';
 import type { Session } from '$lib/api/client';
 import type { ViewType } from '$lib/stores/layout.svelte';
 import { createSearchSources, searchPages, matches, type SearchGroup, type SearchItem } from '$lib/api/search';

 interface Props {
  visible: boolean;
  sessions: Session[];
  currentProjectId?: string;
  onClose: () => void;
  onSelectSession: (session: Session) => void;
  onSelectAgent: (id: string) => void;
  onOpenFeature?: (view: ViewType, itemId?: string) => void;
  onNewChat: () => void;
  onOpenSettings: () => void;
  onImportSession?: () => void;
  onManageTags?: () => void;
  onManageKnowledge?: () => void;
 }
 let { visible, currentProjectId, onClose, onSelectSession, onSelectAgent, onOpenFeature,
  onNewChat, onOpenSettings, onImportSession, onManageTags, onManageKnowledge }: Props = $props();
 let query = $state('');
 let selectedIndex = $state(0);
 let input = $state<HTMLInputElement>();
 let list = $state<HTMLDivElement>();
 let results = $state<SearchGroup[]>([]);
 let failures = $state<string[]>([]);
 let searching = $state(false);
 let sources = $state.raw(createSearchSources());
 const trimmed = $derived(query.trim());
 const actions = $derived<SearchItem[]>([
  { id: 'new', label: 'New conversation', action: onNewChat },
  { id: 'settings', label: 'Open settings', action: onOpenSettings },
  ...(onImportSession ? [{ id: 'import', label: 'Import session', action: onImportSession }] : []),
  ...(onManageTags ? [{ id: 'tags', label: 'Manage session tags', action: onManageTags }] : []),
  ...(currentProjectId && onManageKnowledge ? [{ id: 'knowledge', label: 'Manage project knowledge', action: onManageKnowledge }] : [])
 ]);
 const groups = $derived<SearchGroup[]>([
  ...results,
  ...(trimmed && onOpenFeature ? [{ id: 'pages', label: 'App pages', items: searchPages.filter(item => matches(item, trimmed)) }] : []),
  { id: 'actions', label: 'Quick actions', items: actions.filter(item => !trimmed || matches(item, trimmed)) }
 ].filter(group => group.items.length));
 const items = $derived(groups.flatMap(group => group.items));

 $effect(() => {
  if (!visible) return;
  sources = createSearchSources(currentProjectId);
  query = '';
  selectedIndex = 0;
  tick().then(() => input?.focus());
 });

 $effect(() => {
  const text = trimmed;
  const open = visible;
  // Cleanup invalidates requests immediately, including the debounce window,
  // clearing the query and closing the palette.
  let cancelled = false;
  results = [];
  failures = [];
  selectedIndex = 0;
  searching = open && !!text;
  if (!open || !text) return;
  const currentSources = sources;
  const timer = setTimeout(async () => {
   const settled = await Promise.allSettled(currentSources.map(source => source.load(text)));
   if (cancelled) return;
   results = settled.flatMap((result, index) => result.status === 'fulfilled'
    ? [{ id: currentSources[index].id, label: currentSources[index].label, items: result.value }] : []);
   failures = settled.flatMap((result, index) => result.status === 'rejected' ? [currentSources[index].label] : []);
   searching = false;
  }, 250);
  return () => { cancelled = true; clearTimeout(timer); };
 });

 function select(item: SearchItem) {
  if (item.session) onSelectSession(item.session);
  else if (item.action) item.action();
  else if (item.view === 'profile' && item.itemId) onSelectAgent(item.itemId);
  else if (item.view) onOpenFeature?.(item.view, item.itemId);
  onClose();
 }
 async function handleKeyDown(event: KeyboardEvent) {
  if (!visible) return;
  if (event.key === 'Escape') { event.preventDefault(); onClose(); }
  else if (event.key === 'Enter') { event.preventDefault(); if (items[selectedIndex]) select(items[selectedIndex]); }
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
   event.preventDefault();
   if (!items.length) return;
   selectedIndex = (selectedIndex + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
   await tick();
   list?.querySelector(`[data-index="${selectedIndex}"]`)?.scrollIntoView({ block: 'nearest' });
  }
 }
 function indexOf(groupIndex: number, index: number) {
  return groups.slice(0, groupIndex).reduce((count, group) => count + group.items.length, index);
 }
</script>

<svelte:window onkeydown={handleKeyDown} />
{#if visible}
 <div class="fixed inset-0 z-50 flex items-start justify-center pt-[10vh] px-3" role="dialog" aria-modal="true" aria-label="Search everything" tabindex="-1">
  <button class="absolute inset-0 w-full h-full bg-black/60 backdrop-blur-xs border-none p-0 cursor-default" onclick={onClose} tabindex="-1" aria-label="Close search"></button>
  <div class="relative w-full max-w-xl bg-card border border-border rounded-xl overflow-hidden" style="box-shadow: var(--shadow-l)">
   <div class="flex items-center gap-3 px-4 py-3 border-b border-border">
    <Search size={20} class="text-muted-foreground shrink-0" />
    <input bind:this={input} bind:value={query} aria-label="Search everything" placeholder="Search everything…" class="min-w-0 flex-1 bg-transparent text-foreground placeholder-muted-foreground focus:outline-hidden text-base" />
    <kbd class="text-xs text-muted-foreground">esc</kbd>
   </div>
   <div bind:this={list} class="max-h-[65vh] overflow-y-auto" aria-busy={searching}>
    {#if !trimmed}<p class="px-4 py-3 text-sm text-muted-foreground">Find session messages, agents, MCP servers, plugins, notes and docs.</p>{/if}
    {#if searching}<p role="status" class="px-4 py-3 text-sm text-muted-foreground">Searching…</p>{/if}
    {#each groups as group, groupIndex (group.id)}
     <section aria-label={group.label}>
      <h3 class="px-4 py-2 text-xs font-medium text-muted-foreground uppercase tracking-wider border-t border-border">{group.label} <span class="opacity-60">{group.items.length}</span></h3>
      {#each group.items as item, index (item.id)}
       {@const globalIndex = indexOf(groupIndex, index)}
       <button type="button" data-index={globalIndex} class="spotlight-item w-full px-4 py-2.5 flex items-start gap-3 text-left transition-colors {selectedIndex === globalIndex ? 'selected' : ''}" onmouseenter={() => selectedIndex = globalIndex} onclick={() => select(item)}>
        <Search size={16} class="mt-1 text-muted-foreground shrink-0" />
        <span class="flex-1 min-w-0">
         <span class="block text-sm text-foreground truncate">{item.label}</span>
         {#if item.description}<span class="block text-xs text-muted-foreground line-clamp-2 mt-0.5">{item.session ? item.description.replace(/<\/?b>/g, '').replace(/\*\*/g, '') : item.description}</span>{/if}
        </span>
        <ArrowUpRight size={14} class="mt-1 text-muted-foreground shrink-0" />
       </button>
      {/each}
     </section>
    {/each}
    {#if trimmed && !searching && !items.length}<p class="px-4 py-8 text-center text-muted-foreground">No results found.</p>{/if}
    {#if failures.length}<p role="status" class="px-4 py-3 text-sm text-muted-foreground">Could not search: {failures.join(', ')}. Try again.</p>{/if}
   </div>
   <div class="px-4 py-2 border-t border-border text-xs text-muted-foreground">↑ ↓ navigate · Enter open · Esc close</div>
  </div>
 </div>
{/if}

<style>
  .spotlight-item:hover {
    background: var(--hover-overlay);
  }

  .spotlight-item.selected {
    background: color-mix(in oklch, var(--selection) 10%, var(--card));
  }

  .spotlight-item:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: -2px;
  }
</style>