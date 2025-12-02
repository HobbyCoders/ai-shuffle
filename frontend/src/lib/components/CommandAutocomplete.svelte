<script lang="ts">
  import { onMount } from 'svelte';

  interface Command {
    name: string;
    display: string;
    description: string;
    argument_hint?: string;
    type: 'custom' | 'interactive' | 'sdk_builtin';
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
  let listElement: HTMLUListElement;

  // Fetch commands on mount and when projectId changes
  $effect(() => {
    if (visible) {
      fetchCommands();
    }
  });

  // Filter commands based on input
  $effect(() => {
    if (!inputValue.startsWith('/')) {
      filteredCommands = [];
      return;
    }

    const query = inputValue.slice(1).toLowerCase();
    filteredCommands = commands.filter(cmd =>
      cmd.name.toLowerCase().includes(query) ||
      cmd.description.toLowerCase().includes(query)
    );
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
  <div class="absolute bottom-full left-0 right-0 mb-2 bg-gray-800 border border-gray-700 rounded-lg shadow-xl max-h-64 overflow-hidden z-50">
    <div class="px-3 py-2 border-b border-gray-700 bg-gray-750">
      <span class="text-xs text-gray-400">
        {filteredCommands.length} command{filteredCommands.length !== 1 ? 's' : ''} available
      </span>
    </div>

    <ul bind:this={listElement} class="overflow-y-auto max-h-48">
      {#each filteredCommands as command, index}
        <li>
          <button
            type="button"
            class="w-full px-3 py-2 flex items-start gap-3 text-left hover:bg-gray-700 transition-colors {index === selectedIndex ? 'bg-gray-700' : ''}"
            onclick={() => handleSelect(command)}
            onmouseenter={() => selectedIndex = index}
          >
            <div class="flex-shrink-0">
              <span class="text-blue-400 font-mono text-sm">{command.display}</span>
              {#if command.argument_hint}
                <span class="text-gray-500 font-mono text-xs ml-1">{command.argument_hint}</span>
              {/if}
            </div>
            <div class="flex-1 min-w-0">
              <p class="text-sm text-gray-300 truncate">{command.description}</p>
            </div>
            <div class="flex-shrink-0">
              {#if command.type === 'interactive'}
                <span class="px-1.5 py-0.5 text-xs bg-purple-900/50 text-purple-300 rounded">
                  interactive
                </span>
              {:else if command.type === 'sdk_builtin'}
                <span class="px-1.5 py-0.5 text-xs bg-green-900/50 text-green-300 rounded">
                  builtin
                </span>
              {:else}
                <span class="px-1.5 py-0.5 text-xs bg-blue-900/50 text-blue-300 rounded">
                  custom
                </span>
              {/if}
            </div>
          </button>
        </li>
      {/each}
    </ul>

    <div class="px-3 py-1.5 border-t border-gray-700 bg-gray-750 text-xs text-gray-500">
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mr-1">Tab</kbd> or
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mx-1">Enter</kbd> to select
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mx-1">Esc</kbd> to close
    </div>
  </div>
{/if}

<style>
  .bg-gray-750 {
    background-color: rgb(42, 42, 54);
  }
</style>
