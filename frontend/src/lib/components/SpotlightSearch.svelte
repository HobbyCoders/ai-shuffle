<script lang="ts">
  import { Users } from 'lucide-svelte';
  import type { Session } from '$lib/api/client';
  import { searchSessionsPaginated, type SessionSearchResult } from '$lib/api/sessions';
  import { api } from '$lib/api/client';
  import { sanitizeHtml } from '$lib/utils/sanitize';

  interface Agent {
    id: string;
    name: string;
    description: string;
  }

  interface Props {
    visible: boolean;
    sessions: Session[];
    currentProjectId?: string;
    onClose: () => void;
    onSelectSession: (session: Session) => void;
    onSelectAgent: (id: string) => void;
    onNewChat: () => void;
    onOpenSettings: () => void;
  }

  let {
    visible,
    sessions,
    currentProjectId,
    onClose,
    onSelectSession,
    onSelectAgent,
    onNewChat,
    onOpenSettings
  }: Props = $props();

  let searchQuery = $state('');
  let selectedIndex = $state(0);
  let searchInput = $state<HTMLInputElement | undefined>(undefined);
  let listElement = $state<HTMLDivElement | undefined>(undefined);

  // Session search (server-side FTS, debounced). Empty query falls back to the
  // 5 most-recent sessions from the `sessions` prop (instant, no network).
  let sessionResults = $state<SessionSearchResult[]>([]);
  let isSearchingSessions = $state(false);
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  // Monotonic token so a stale in-flight search can't clobber a newer one.
  let searchToken = 0;

  // Agents: loaded once when the palette opens, filtered client-side.
  let agents = $state<Agent[]>([]);
  let dataLoaded = false;

  // Quick actions (derived so prop references stay reactive)
  const quickActions = $derived([
    { id: 'new-chat', label: 'New conversation', icon: 'plus', action: onNewChat },
    { id: 'settings', label: 'Open settings', icon: 'settings', action: onOpenSettings }
  ]);

  const trimmedQuery = $derived(searchQuery.trim());

  // Sessions to render: recent-5 when empty, otherwise server results.
  const displaySessions = $derived.by<SessionSearchResult[]>(() => {
    if (!trimmedQuery) {
      return sessions.slice(0, 5).map((s) => ({
        ...s,
        match_type: 'title' as const,
        match_snippet: null,
        match_time: null
      }));
    }
    return sessionResults;
  });

  const filteredAgents = $derived.by(() => {
    if (!trimmedQuery) return agents;
    const q = trimmedQuery.toLowerCase();
    return agents.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        (a.description ?? '').toLowerCase().includes(q)
    );
  });

  const filteredQuickActions = $derived.by(() => {
    if (!trimmedQuery) return quickActions;
    const q = trimmedQuery.toLowerCase();
    return quickActions.filter((action) => action.label.toLowerCase().includes(q));
  });

  // Ordered group list drives both rendering and keyboard navigation.
  type ItemType = 'session' | 'agent' | 'action';
  const groups = $derived<Array<{ type: ItemType; items: unknown[] }>>([
    { type: 'session', items: displaySessions },
    { type: 'agent', items: filteredAgents },
    { type: 'action', items: filteredQuickActions }
  ]);

  const allItems = $derived.by(() => {
    const items: Array<{ type: ItemType; data: any }> = [];
    for (const group of groups) {
      for (const data of group.items) {
        items.push({ type: group.type, data });
      }
    }
    return items;
  });

  // Global (flat) index for a group's local item, used by keyboard nav + data-index.
  function getGlobalIndex(type: ItemType, localIndex: number): number {
    let offset = 0;
    for (const group of groups) {
      if (group.type === type) break;
      offset += group.items.length;
    }
    return offset + localIndex;
  }

  // Reset + focus + one-time data load when the palette opens.
  $effect(() => {
    if (visible) {
      searchQuery = '';
      selectedIndex = 0;
      sessionResults = [];
      isSearchingSessions = false;
      loadAuxData();
      setTimeout(() => searchInput?.focus(), 50);
    }
  });

  // Keep the selection in-bounds as results change.
  $effect(() => {
    if (selectedIndex >= allItems.length) {
      selectedIndex = allItems.length > 0 ? allItems.length - 1 : 0;
    }
  });

  // Debounced server-side session search on query change.
  $effect(() => {
    const q = searchQuery;
    if (debounceTimer) clearTimeout(debounceTimer);
    if (!q.trim()) {
      sessionResults = [];
      isSearchingSessions = false;
      return;
    }
    isSearchingSessions = true;
    debounceTimer = setTimeout(() => runSessionSearch(q), 300);
    return () => {
      if (debounceTimer) clearTimeout(debounceTimer);
    };
  });

  async function loadAuxData() {
    if (dataLoaded) return;
    dataLoaded = true;
    try {
      agents = await api.get<Agent[]>('/agents');
    } catch (error) {
      console.error('Failed to load agents:', error);
      agents = [];
    }
  }

  async function runSessionSearch(query: string) {
    const token = ++searchToken;
    try {
      const res = await searchSessionsPaginated(query, {
        projectId: currentProjectId,
        limit: 10
      });
      if (token !== searchToken) return; // stale
      sessionResults = res.items;
    } catch (error) {
      if (token !== searchToken) return;
      console.error('Failed to search sessions:', error);
      sessionResults = [];
    } finally {
      if (token === searchToken) isSearchingSessions = false;
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (!visible) return;

    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        onClose();
        break;

      case 'ArrowDown':
        event.preventDefault();
        if (allItems.length > 0) {
          selectedIndex = (selectedIndex + 1) % allItems.length;
          scrollToSelected();
        }
        break;

      case 'ArrowUp':
        event.preventDefault();
        if (allItems.length > 0) {
          selectedIndex = selectedIndex === 0 ? allItems.length - 1 : selectedIndex - 1;
          scrollToSelected();
        }
        break;

      case 'Enter':
        event.preventDefault();
        selectCurrentItem();
        break;
    }
  }

  function scrollToSelected() {
    if (listElement) {
      const selected = listElement.querySelector(`[data-index="${selectedIndex}"]`) as HTMLElement;
      if (selected) {
        selected.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function selectCurrentItem() {
    if (selectedIndex < 0 || selectedIndex >= allItems.length) return;

    const item = allItems[selectedIndex];
    switch (item.type) {
      case 'session':
        onSelectSession(item.data);
        break;
      case 'agent':
        onSelectAgent(item.data.id);
        break;
      case 'action':
        item.data.action();
        break;
    }
    onClose();
  }

  function renderSnippet(snippet: string): string {
    // Match snippets carry **bold** markers around the hit — render them as marks.
    return sanitizeHtml(
      snippet.replace(
        /\*\*(.*?)\*\*/g,
        '<mark class="bg-yellow-500/30 text-foreground px-0.5 rounded-sm">$1</mark>'
      )
    );
  }

  function formatTimeAgo(dateStr: string): string {
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins} min ago`;
    if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
    if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
    return date.toLocaleDateString();
  }

  const hasAnyItems = $derived(allItems.length > 0);
</script>

<svelte:window onkeydown={handleKeyDown} />

{#if visible}
  <!-- Backdrop with blur -->
  <div
    class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-start justify-center pt-[15vh] max-sm:pt-[10vh]"
    role="dialog"
    aria-modal="true"
    aria-label="Spotlight search"
    tabindex="-1"
  >
    <button
      class="absolute inset-0 w-full h-full bg-black/60 backdrop-blur-xs border-none p-0 cursor-default"
      onclick={onClose}
      tabindex="-1"
      aria-label="Close dialog"
    ></button>
    <!-- Modal -->
    <div
      class="relative w-full max-w-xl bg-card border border-border rounded-xl overflow-hidden"
      style="box-shadow: var(--shadow-l); z-index: 1;"
    >
      <!-- Search Input -->
      <div class="flex items-center gap-3 px-4 py-3 border-b border-border">
        <svg class="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <input
          bind:this={searchInput}
          bind:value={searchQuery}
          type="text"
          placeholder="Search chats, agents..."
          class="flex-1 bg-transparent text-foreground placeholder-muted-foreground focus:outline-hidden text-base"
        />
        <kbd class="hidden sm:inline-flex items-center px-2 py-1 text-xs text-muted-foreground bg-muted rounded-sm">
          esc
        </kbd>
      </div>

      <!-- Results -->
      <div bind:this={listElement} class="max-h-[60vh] overflow-y-auto">
        {#if !hasAnyItems && !isSearchingSessions}
          <div class="px-4 py-8 text-center text-muted-foreground">
            <p>No results found</p>
          </div>
        {:else}
          <!-- Chats -->
          {#if displaySessions.length > 0}
            <div class="px-4 py-2">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Chats
              </h3>
            </div>
            {#each displaySessions as session, index}
              {@const globalIndex = getGlobalIndex('session', index)}
              {@const matchCount = session.match_count ?? 1}
              <button
                type="button"
                data-index={globalIndex}
                class="spotlight-item w-full px-4 py-2.5 flex items-start gap-3 text-left transition-colors {selectedIndex === globalIndex ? 'selected' : ''}"
                onclick={() => { onSelectSession(session); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                <svg class="w-5 h-5 mt-0.5 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                </svg>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2">
                    <p class="text-sm text-foreground truncate">
                      {session.title || 'Untitled session'}
                    </p>
                    {#if matchCount > 1}
                      <span class="shrink-0 text-[0.65rem] px-1.5 py-0.5 bg-muted rounded-sm text-muted-foreground">
                        {matchCount} matches
                      </span>
                    {/if}
                  </div>
                  {#if session.match_snippet}
                    <p class="text-xs text-muted-foreground truncate mt-0.5">
                      {@html renderSnippet(session.match_snippet)}
                    </p>
                  {/if}
                </div>
                <span class="text-xs text-muted-foreground shrink-0 mt-0.5">
                  {formatTimeAgo(session.match_time || session.updated_at)}
                </span>
              </button>
            {/each}
          {/if}

          <!-- Loading spinner row while a session search is in flight -->
          {#if isSearchingSessions}
            <div class="px-4 py-3 flex items-center gap-3 text-muted-foreground">
              <div class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-primary"></div>
              <span class="text-sm">Searching…</span>
            </div>
          {/if}

          <!-- Agents -->
          {#if filteredAgents.length > 0}
            <div class="px-4 py-2 {displaySessions.length > 0 ? 'border-t border-border' : ''}">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Agents
              </h3>
            </div>
            {#each filteredAgents as agent, index}
              {@const globalIndex = getGlobalIndex('agent', index)}
              <button
                type="button"
                data-index={globalIndex}
                class="spotlight-item w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors {selectedIndex === globalIndex ? 'selected' : ''}"
                onclick={() => { onSelectAgent(agent.id); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                <Users class="w-5 h-5 text-blue-400 shrink-0" strokeWidth={1.8} />
                <div class="flex-1 min-w-0">
                  <p class="text-sm text-foreground truncate">{agent.name}</p>
                  {#if agent.description}
                    <p class="text-xs text-muted-foreground truncate">{agent.description}</p>
                  {/if}
                </div>
              </button>
            {/each}
          {/if}

          <!-- Quick Actions -->
          {#if filteredQuickActions.length > 0}
            <div class="px-4 py-2 {displaySessions.length > 0 || filteredAgents.length > 0 ? 'border-t border-border' : ''}">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Quick Actions
              </h3>
            </div>
            {#each filteredQuickActions as action, index}
              {@const globalIndex = getGlobalIndex('action', index)}
              <button
                type="button"
                data-index={globalIndex}
                class="spotlight-item w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors {selectedIndex === globalIndex ? 'selected' : ''}"
                onclick={() => { action.action(); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                {#if action.icon === 'plus'}
                  <svg class="w-5 h-5 text-green-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                  </svg>
                {:else if action.icon === 'settings'}
                  <svg class="w-5 h-5 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                  </svg>
                {/if}
                <span class="text-sm text-foreground">{action.label}</span>
              </button>
            {/each}
          {/if}
        {/if}
      </div>

      <!-- Footer -->
      <div class="px-4 py-2 border-t border-border bg-muted/30 flex items-center justify-between text-xs text-muted-foreground">
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1">
            <kbd class="px-1.5 py-0.5 bg-muted rounded-sm">
              <svg class="w-3 h-3 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
              </svg>
            </kbd>
            <kbd class="px-1.5 py-0.5 bg-muted rounded-sm">
              <svg class="w-3 h-3 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7" />
              </svg>
            </kbd>
            navigate
          </span>
          <span class="flex items-center gap-1">
            <kbd class="px-1.5 py-0.5 bg-muted rounded-sm">enter</kbd>
            select
          </span>
        </div>
        <span class="flex items-center gap-1">
          <kbd class="px-1.5 py-0.5 bg-muted rounded-sm">esc</kbd>
          close
        </span>
      </div>
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
