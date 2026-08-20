<script lang="ts">
  import './autocomplete/autocomplete-design-system.css';

  interface Command {
    name: string;
    display: string;
    description: string;
    argument_hint?: string;
    type: 'custom' | 'interactive' | 'sdk_builtin' | 'plugin' | 'skill';
    source?: string;
  }

  interface Props {
    inputValue: string;
    projectId?: string;
    worktreeId?: string;
    onSelect: (command: Command, andSubmit?: boolean) => void;
    onClose: () => void;
    visible: boolean;
  }

  let { inputValue, projectId, worktreeId, onSelect, onClose, visible }: Props = $props();

  let commands = $state<Command[]>([]);
  let filteredCommands = $state<Command[]>([]);
  let selectedIndex = $state(0);
  let loading = $state(false);
  let listElement = $state<HTMLUListElement | null>(null);

  // Current query for match highlighting
  let currentQuery = $derived(inputValue.startsWith('/') ? inputValue.slice(1).toLowerCase().trim() : '');

  // Section breaks by command type (only shown when no filter query)
  let sectionBreaks = $derived.by(() => {
    if (currentQuery) return new Map<number, string>();
    const breaks = new Map<number, string>();
    const typeLabels: Record<string, string> = {
      sdk_builtin: 'Built-in',
      custom: 'Custom',
      skill: 'Skills',
      plugin: 'Plugin',
      interactive: 'Interactive'
    };
    let lastType = '';
    filteredCommands.forEach((cmd, i) => {
      if (cmd.type !== lastType) {
        breaks.set(i, typeLabels[cmd.type] || cmd.type);
        lastType = cmd.type;
      }
    });
    return breaks;
  });

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

    const query = inputValue.slice(1).toLowerCase().trim();

    if (!query) {
      // Sort by type when showing all commands
      const typeOrder = ['sdk_builtin', 'custom', 'skill', 'plugin', 'interactive'];
      filteredCommands = [...commands].sort((a, b) => {
        const aOrder = typeOrder.indexOf(a.type);
        const bOrder = typeOrder.indexOf(b.type);
        if (aOrder !== bOrder) return aOrder - bOrder;
        return a.name.localeCompare(b.name);
      });
      selectedIndex = 0;
      return;
    }

    const matches = commands.filter(cmd =>
      cmd.name.toLowerCase().includes(query) ||
      cmd.description.toLowerCase().includes(query)
    );

    filteredCommands = matches.sort((a, b) => {
      const aName = a.name.toLowerCase();
      const bName = b.name.toLowerCase();

      if (aName === query && bName !== query) return -1;
      if (bName === query && aName !== query) return 1;

      const aStartsWith = aName.startsWith(query);
      const bStartsWith = bName.startsWith(query);
      if (aStartsWith && !bStartsWith) return -1;
      if (bStartsWith && !aStartsWith) return 1;

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
      if (worktreeId) {
        params.set('worktree_id', worktreeId);
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

  // Highlight matching text
  function highlightMatch(text: string, query: string): Array<{text: string, matched: boolean}> {
    if (!query) return [{ text, matched: false }];
    const lowerText = text.toLowerCase();
    const lowerQuery = query.toLowerCase();
    const idx = lowerText.indexOf(lowerQuery);
    if (idx === -1) return [{ text, matched: false }];
    return [
      { text: text.slice(0, idx), matched: false },
      { text: text.slice(idx, idx + query.length), matched: true },
      { text: text.slice(idx + query.length), matched: false }
    ].filter(s => s.text.length > 0);
  }

  function getTypeName(type: string): string {
    switch (type) {
      case 'plugin': return 'plugin';
      case 'interactive': return 'interactive';
      case 'sdk_builtin': return 'builtin';
      case 'skill': return 'skill';
      default: return 'custom';
    }
  }

  /**
   * Handle keyboard events for the autocomplete.
   * Returns true if the event was handled (parent should not process it).
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

      case 'Tab':
        if (filteredCommands[selectedIndex]) {
          event.preventDefault();
          event.stopPropagation();
          onSelect(filteredCommands[selectedIndex]);
          return true;
        }
        return false;

      case 'Enter':
        // Select the highlighted command and submit it
        if (filteredCommands[selectedIndex]) {
          event.preventDefault();
          onSelect(filteredCommands[selectedIndex], true);
          return true;
        }
        onClose();
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
      const selected = listElement.querySelector(`[data-index="${selectedIndex}"]`) as HTMLElement;
      if (selected) {
        selected.scrollIntoView({ block: 'nearest' });
      }
    }
  }
</script>

{#if visible && filteredCommands.length > 0}
  <div class="autocomplete-panel command-autocomplete-panel">
    <div class="autocomplete-header">
      <span class="autocomplete-count">
        {filteredCommands.length} command{filteredCommands.length !== 1 ? 's' : ''}
      </span>
      <span class="autocomplete-hint-text">Type to filter</span>
    </div>

    <ul bind:this={listElement} class="autocomplete-list" role="listbox" aria-label="Commands" style="max-height: 14rem;">
      {#each filteredCommands as command, index}
        {#if sectionBreaks.has(index)}
          <li class="autocomplete-section-header" role="presentation">
            {sectionBreaks.get(index)}
          </li>
        {/if}
        <li data-index={index} role="option" aria-selected={index === selectedIndex}>
          <button
            type="button"
            class="autocomplete-item"
            class:selected={index === selectedIndex}
            onclick={() => onSelect(command)}
            onmouseenter={() => selectedIndex = index}
          >
            <div class="autocomplete-item-content">
              <div class="autocomplete-item-name">
                <span class="command-name">
                  {#each highlightMatch(command.display, currentQuery) as seg}
                    {#if seg.matched}<span class="autocomplete-match">{seg.text}</span>{:else}{seg.text}{/if}
                  {/each}
                </span>
                {#if command.argument_hint}
                  <span class="command-hint">{command.argument_hint}</span>
                {/if}
              </div>
              <div class="autocomplete-item-description">
                {#each highlightMatch(command.description, currentQuery) as seg}
                  {#if seg.matched}<span class="autocomplete-match">{seg.text}</span>{:else}{seg.text}{/if}
                {/each}
              </div>
            </div>
            <span class="autocomplete-badge type-{command.type === 'sdk_builtin' ? 'builtin' : command.type}">
              {getTypeName(command.type)}
            </span>
          </button>
        </li>
      {/each}
    </ul>

    <div class="autocomplete-footer">
      <span><kbd class="autocomplete-kbd">↑↓</kbd> <span class="autocomplete-hint-text">navigate</span></span>
      <span><kbd class="autocomplete-kbd">Tab</kbd> <span class="autocomplete-hint-text">select</span></span>
      <span><kbd class="autocomplete-kbd">Enter</kbd> <span class="autocomplete-hint-text">send</span></span>
      <span><kbd class="autocomplete-kbd">Esc</kbd> <span class="autocomplete-hint-text">close</span></span>
    </div>
  </div>
{:else if visible && filteredCommands.length === 0 && !loading && commands.length > 0}
  <div class="autocomplete-panel command-autocomplete-panel">
    <div class="autocomplete-header">
      <span class="autocomplete-count">0 commands</span>
    </div>
    <div class="autocomplete-empty">
      <svg class="autocomplete-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
      </svg>
      <span>No matching commands</span>
    </div>
    <div class="autocomplete-footer">
      <span><kbd class="autocomplete-kbd">Esc</kbd> <span class="autocomplete-hint-text">close</span></span>
    </div>
  </div>
{/if}

<style>
  .command-autocomplete-panel {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: 8px;
    max-height: 20rem;
    z-index: 50;
    background: var(--popover, var(--card));
  }

  /* Mobile: break out of textarea-wrapper to span full chat island width */
  @media (max-width: 640px) {
    .command-autocomplete-panel {
      left: -15px; /* -(island .input-area padding 14px + island border 1px) */
      right: auto;
      width: calc(100vw - 1.5rem); /* viewport minus chat-input-wrapper horizontal padding */
    }
  }
</style>
