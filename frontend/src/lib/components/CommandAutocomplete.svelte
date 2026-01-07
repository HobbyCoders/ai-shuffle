<script lang="ts">
  import { onMount } from 'svelte';

  interface Command {
    name: string;
    display: string;
    description: string;
    argument_hint?: string;
    type: 'custom' | 'interactive' | 'sdk_builtin' | 'plugin';
    source?: string;
  }

  interface Props {
    inputValue: string;
    projectId?: string;
    onSelect: (command: Command) => void;
    onClose: () => void;
    visible: boolean;
    onKeyDown?: (event: KeyboardEvent) => boolean; // Returns true if event was handled
  }

  let { inputValue, projectId, onSelect, onClose, visible, onKeyDown }: Props = $props();

  let commands = $state<Command[]>([]);
  let filteredCommands = $state<Command[]>([]);
  let selectedIndex = $state(0);
  let loading = $state(false);
  let listElement = $state<HTMLUListElement | null>(null);

  // Fetch commands on mount and when projectId changes
  $effect(() => {
    if (visible) {
      fetchCommands();
    }
  });

  // Filter and sort commands based on input
  $effect(() => {
    if (!inputValue.startsWith('/')) {
      filteredCommands = [];
      return;
    }

    // Get the query after the slash, trimming any trailing spaces for matching
    const query = inputValue.slice(1).toLowerCase().trim();

    // If no query yet, show all commands
    if (!query) {
      filteredCommands = [...commands];
      selectedIndex = 0;
      return;
    }

    // Filter commands that match the query
    const matches = commands.filter(cmd =>
      cmd.name.toLowerCase().includes(query) ||
      cmd.description.toLowerCase().includes(query)
    );

    // Sort by relevance:
    // 1. Exact match first
    // 2. Starts with query
    // 3. Contains query (alphabetically)
    filteredCommands = matches.sort((a, b) => {
      const aName = a.name.toLowerCase();
      const bName = b.name.toLowerCase();

      // Exact match gets highest priority
      if (aName === query && bName !== query) return -1;
      if (bName === query && aName !== query) return 1;

      // Prefix match gets second priority
      const aStartsWith = aName.startsWith(query);
      const bStartsWith = bName.startsWith(query);
      if (aStartsWith && !bStartsWith) return -1;
      if (bStartsWith && !aStartsWith) return 1;

      // Otherwise sort alphabetically
      return aName.localeCompare(bName);
    });

    selectedIndex = 0;
  });

  async function fetchCommands() {
    loading = true;
    try {
      const params = new URLSearchParams();
      if (projectId) {
        params.set('project_id', projectId);
      }

      const response = await fetch(`/api/v1/commands/?${params}`, {
        credentials: 'include'
      });

      if (response.ok) {
        const data = await response.json();
        commands = data.commands;
      }
    } catch (error) {
      console.error('Failed to fetch commands:', error);
    } finally {
      loading = false;
    }
  }

  /**
   * Handle keyboard events for the autocomplete.
   * Returns true if the event was handled (parent should not process it).
   * This function is exported so the parent can call it from its keydown handler.
   */
  export function handleKeyDown(event: KeyboardEvent): boolean {
    if (!visible || filteredCommands.length === 0) return false;

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        selectedIndex = Math.min(selectedIndex + 1, filteredCommands.length - 1);
        scrollToSelected();
        return true;

      case 'ArrowUp':
        event.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, 0);
        scrollToSelected();
        return true;

      case 'Enter':
      case 'Tab':
        if (filteredCommands[selectedIndex]) {
          event.preventDefault();
          event.stopPropagation();
          onSelect(filteredCommands[selectedIndex]);
          return true;
        }
        return false;

      case 'Escape':
        event.preventDefault();
        onClose();
        return true;

      default:
        return false;
    }
  }

  function scrollToSelected() {
    if (listElement) {
      const selected = listElement.children[selectedIndex] as HTMLElement;
      if (selected) {
        selected.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function handleSelect(command: Command) {
    onSelect(command);
  }
</script>

{#if visible && filteredCommands.length > 0}
  <div class="absolute bottom-full left-0 right-0 mb-2 bg-popover border border-border rounded-lg shadow-xl max-h-64 overflow-hidden z-50">
    <div class="px-3 py-2 border-b border-border bg-muted">
      <span class="text-xs text-muted-foreground">
        {filteredCommands.length} command{filteredCommands.length !== 1 ? 's' : ''} available
      </span>
    </div>

    <ul bind:this={listElement} class="overflow-y-auto max-h-48">
      {#each filteredCommands as command, index}
        <li>
          <button
            type="button"
            class="w-full px-3 py-2 flex items-start gap-3 text-left hover:bg-accent transition-colors {index === selectedIndex ? 'bg-accent' : ''}"
            onclick={() => handleSelect(command)}
            onmouseenter={() => selectedIndex = index}
          >
            <div class="flex-shrink-0">
              <span class="text-primary font-mono text-sm">{command.display}</span>
              {#if command.argument_hint}
                <span class="text-muted-foreground font-mono text-xs ml-1">{command.argument_hint}</span>
              {/if}
            </div>
            <div class="flex-1 min-w-0">
              <p class="text-sm text-foreground truncate">{command.description}</p>
            </div>
            <div class="flex-shrink-0">
              {#if command.type === 'plugin'}
                <span class="px-1.5 py-0.5 text-xs bg-purple-500/20 text-purple-400 rounded">
                  plugin
                </span>
              {:else if command.type === 'interactive'}
                <span class="px-1.5 py-0.5 text-xs bg-primary/20 text-primary rounded">
                  interactive
                </span>
              {:else if command.type === 'sdk_builtin'}
                <span class="px-1.5 py-0.5 text-xs bg-success/20 text-success rounded">
                  builtin
                </span>
              {:else}
                <span class="px-1.5 py-0.5 text-xs bg-info/20 text-info rounded">
                  custom
                </span>
              {/if}
            </div>
          </button>
        </li>
      {/each}
    </ul>

    <div class="px-3 py-1.5 border-t border-border bg-muted text-xs text-muted-foreground">
      <kbd class="px-1 py-0.5 bg-secondary rounded mr-1">Tab</kbd> or
      <kbd class="px-1 py-0.5 bg-secondary rounded mx-1">Enter</kbd> to select
      <kbd class="px-1 py-0.5 bg-secondary rounded mx-1">Esc</kbd> to close
    </div>
  </div>
{/if}
