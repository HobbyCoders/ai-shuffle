<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { listCommands, type Command } from '$lib/api/commands';
  import type { Session } from '$lib/api/client';

  interface Props {
    visible: boolean;
    sessions: Session[];
    currentProjectId?: string;
    onClose: () => void;
    onSelectSession: (session: Session) => void;
    onSelectCommand: (command: Command) => void;
    onNewChat: () => void;
    onOpenSettings: () => void;
  }

  let { visible, sessions, currentProjectId, onClose, onSelectSession, onSelectCommand, onNewChat, onOpenSettings }: Props = $props();

  let searchQuery = $state('');
  let commands = $state<Command[]>([]);
  let selectedIndex = $state(0);
  let searchInput: HTMLInputElement;
  let listElement: HTMLDivElement;

  // Quick actions
  const quickActions = [
    { id: 'new-chat', label: 'New conversation', icon: 'plus', action: onNewChat },
    { id: 'settings', label: 'Open settings', icon: 'settings', action: onOpenSettings }
  ];

  // Computed filtered items
  let filteredSessions = $derived.by(() => {
    if (!searchQuery.trim()) {
      return sessions.slice(0, 5); // Show 5 most recent
    }
    const query = searchQuery.toLowerCase();
    return sessions.filter(s =>
      s.title?.toLowerCase().includes(query) ||
      s.id.toLowerCase().includes(query)
    ).slice(0, 10);
  });

  let filteredCommands = $derived.by(() => {
    if (!searchQuery.trim()) {
      return commands.slice(0, 5);
    }
    const query = searchQuery.toLowerCase();
    return commands.filter(cmd =>
      cmd.name.toLowerCase().includes(query) ||
      cmd.description.toLowerCase().includes(query)
    ).slice(0, 10);
  });

  let filteredQuickActions = $derived.by(() => {
    if (!searchQuery.trim()) {
      return quickActions;
    }
    const query = searchQuery.toLowerCase();
    return quickActions.filter(action =>
      action.label.toLowerCase().includes(query)
    );
  });

  // Total items for keyboard navigation
  let allItems = $derived.by(() => {
    const items: Array<{ type: 'session' | 'command' | 'action'; data: any }> = [];

    filteredSessions.forEach(s => items.push({ type: 'session', data: s }));
    filteredCommands.forEach(c => items.push({ type: 'command', data: c }));
    filteredQuickActions.forEach(a => items.push({ type: 'action', data: a }));

    return items;
  });

  // Load commands when visible changes
  $effect(() => {
    if (visible) {
      fetchCommands();
      searchQuery = '';
      selectedIndex = 0;
      // Focus search input
      setTimeout(() => {
        searchInput?.focus();
      }, 50);
    }
  });

  // Reset selected index when items change
  $effect(() => {
    if (allItems.length > 0 && selectedIndex >= allItems.length) {
      selectedIndex = 0;
    }
  });

  async function fetchCommands() {
    try {
      const result = await listCommands(currentProjectId);
      commands = result.commands;
    } catch (error) {
      console.error('Failed to fetch commands:', error);
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
      case 'command':
        onSelectCommand(item.data);
        break;
      case 'action':
        item.data.action();
        break;
    }
    onClose();
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      onClose();
    }
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

  // Calculate item index for a given section and local index
  function getGlobalIndex(type: 'session' | 'command' | 'action', localIndex: number): number {
    let offset = 0;
    if (type === 'command') {
      offset = filteredSessions.length;
    } else if (type === 'action') {
      offset = filteredSessions.length + filteredCommands.length;
    }
    return offset + localIndex;
  }
</script>

<svelte:window on:keydown={handleKeyDown} />

{#if visible}
  <!-- Backdrop with blur -->
  <div
    class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-start justify-center pt-[15vh] max-sm:pt-[10vh] bg-black/60 backdrop-blur-sm"
    onclick={handleBackdropClick}
    role="dialog"
    aria-modal="true"
    aria-label="Spotlight search"
  >
    <!-- Modal -->
    <div
      class="w-full max-w-xl bg-card border border-border rounded-xl overflow-hidden"
      style="box-shadow: var(--shadow-l);"
    >
      <!-- Search Input -->
      <div class="flex items-center gap-3 px-4 py-3 border-b border-border">
        <svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <input
          bind:this={searchInput}
          bind:value={searchQuery}
          type="text"
          placeholder="Search sessions, commands, settings..."
          class="flex-1 bg-transparent text-foreground placeholder-muted-foreground focus:outline-none text-base"
        />
        <kbd class="hidden sm:inline-flex items-center px-2 py-1 text-xs text-muted-foreground bg-muted rounded">
          esc
        </kbd>
      </div>

      <!-- Results -->
      <div bind:this={listElement} class="max-h-[60vh] overflow-y-auto">
        {#if allItems.length === 0}
          <div class="px-4 py-8 text-center text-muted-foreground">
            <p>No results found</p>
          </div>
        {:else}
          <!-- Recent Sessions -->
          {#if filteredSessions.length > 0}
            <div class="px-4 py-2">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Recent Sessions
              </h3>
            </div>
            {#each filteredSessions as session, index}
              {@const globalIndex = getGlobalIndex('session', index)}
              <button
                type="button"
                data-index={globalIndex}
                class="w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors hover:bg-accent {selectedIndex === globalIndex ? 'bg-accent' : ''}"
                onclick={() => { onSelectSession(session); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                <svg class="w-5 h-5 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                </svg>
                <div class="flex-1 min-w-0">
                  <p class="text-sm text-foreground truncate">
                    {session.title || 'Untitled session'}
                  </p>
                </div>
                <span class="text-xs text-muted-foreground flex-shrink-0">
                  {formatTimeAgo(session.updated_at)}
                </span>
              </button>
            {/each}
          {/if}

          <!-- Commands -->
          {#if filteredCommands.length > 0}
            <div class="px-4 py-2 {filteredSessions.length > 0 ? 'border-t border-border' : ''}">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Commands
              </h3>
            </div>
            {#each filteredCommands as command, index}
              {@const globalIndex = getGlobalIndex('command', index)}
              <button
                type="button"
                data-index={globalIndex}
                class="w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors hover:bg-accent {selectedIndex === globalIndex ? 'bg-accent' : ''}"
                onclick={() => { onSelectCommand(command); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                {#if command.type === 'interactive'}
                  <svg class="w-5 h-5 text-purple-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                {:else}
                  <svg class="w-5 h-5 text-blue-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                  </svg>
                {/if}
                <div class="flex-1 min-w-0">
                  <span class="text-sm text-foreground font-mono">/{command.name}</span>
                  <span class="text-sm text-muted-foreground ml-2">-</span>
                  <span class="text-sm text-muted-foreground ml-2 truncate">{command.description}</span>
                </div>
              </button>
            {/each}
          {/if}

          <!-- Quick Actions -->
          {#if filteredQuickActions.length > 0}
            <div class="px-4 py-2 {filteredSessions.length > 0 || filteredCommands.length > 0 ? 'border-t border-border' : ''}">
              <h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Quick Actions
              </h3>
            </div>
            {#each filteredQuickActions as action, index}
              {@const globalIndex = getGlobalIndex('action', index)}
              <button
                type="button"
                data-index={globalIndex}
                class="w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors hover:bg-accent {selectedIndex === globalIndex ? 'bg-accent' : ''}"
                onclick={() => { action.action(); onClose(); }}
                onmouseenter={() => selectedIndex = globalIndex}
              >
                {#if action.icon === 'plus'}
                  <svg class="w-5 h-5 text-green-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                  </svg>
                {:else if action.icon === 'settings'}
                  <svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
            <kbd class="px-1.5 py-0.5 bg-muted rounded">
              <svg class="w-3 h-3 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
              </svg>
            </kbd>
            <kbd class="px-1.5 py-0.5 bg-muted rounded">
              <svg class="w-3 h-3 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7" />
              </svg>
            </kbd>
            navigate
          </span>
          <span class="flex items-center gap-1">
            <kbd class="px-1.5 py-0.5 bg-muted rounded">enter</kbd>
            select
          </span>
        </div>
        <span class="flex items-center gap-1">
          <kbd class="px-1.5 py-0.5 bg-muted rounded">esc</kbd>
          close
        </span>
      </div>
    </div>
  </div>
{/if}
