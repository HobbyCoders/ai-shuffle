<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';

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

  // DOM references - not reactive (don't need to trigger re-renders)
  let listElement: HTMLUListElement | null = null;
  let anchorElement = $state<HTMLDivElement | null>(null);

  // Portal state - use regular variables to avoid infinite loops in effects
  let portalContainer: HTMLDivElement | null = null;
  let dropdownPosition = { top: 0, left: 0, width: 0 };
  let lastRenderedCommandsHash = ''; // Track what we've rendered to avoid unnecessary re-renders

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

  // Manage portal lifecycle based on visibility
  $effect(() => {
    if (visible && filteredCommands.length > 0) {
      createPortal();
      // Only render if commands changed
      const commandsHash = filteredCommands.map(c => c.name).join(',');
      if (commandsHash !== lastRenderedCommandsHash) {
        lastRenderedCommandsHash = commandsHash;
        renderPortalContent();
      }
      updatePosition();
    } else {
      removePortal();
      lastRenderedCommandsHash = '';
    }
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

  function updatePosition() {
    if (!anchorElement) return;

    // Get the parent element's bounding rect (the textarea-wrapper)
    const parentEl = anchorElement.parentElement;
    if (!parentEl) return;

    const rect = parentEl.getBoundingClientRect();
    // Position above the parent element
    const newPosition = {
      top: rect.top - 8, // 8px gap above
      left: rect.left,
      width: rect.width
    };

    // Only update if position actually changed
    if (newPosition.top === dropdownPosition.top &&
        newPosition.left === dropdownPosition.left &&
        newPosition.width === dropdownPosition.width) {
      return;
    }

    dropdownPosition = newPosition;

    // Update just the position styles, not the entire content
    const dropdown = portalContainer?.querySelector('.command-autocomplete-dropdown') as HTMLElement;
    if (dropdown) {
      dropdown.style.bottom = `calc(100vh - ${dropdownPosition.top}px)`;
      dropdown.style.left = `${dropdownPosition.left}px`;
      dropdown.style.width = `${dropdownPosition.width}px`;
    } else {
      // No dropdown yet, do full render
      renderPortalContent();
    }
  }

  function createPortal() {
    if (portalContainer) return;

    portalContainer = document.createElement('div');
    portalContainer.className = 'command-autocomplete-portal-root';
    // Set inline styles to ensure pointer-events work (CSS might get tree-shaken)
    portalContainer.style.cssText = 'position: fixed; top: 0; left: 0; z-index: 99999; pointer-events: none;';
    document.body.appendChild(portalContainer);

    // Listen for scroll/resize to update position
    window.addEventListener('scroll', updatePosition, true);
    window.addEventListener('resize', updatePosition);
  }

  function removePortal() {
    if (portalContainer) {
      portalContainer.remove();
      portalContainer = null;
    }
    window.removeEventListener('scroll', updatePosition, true);
    window.removeEventListener('resize', updatePosition);
  }

  function renderPortalContent() {
    if (!portalContainer || filteredCommands.length === 0) return;

    const commandsHtml = filteredCommands.map((command, index) => `
      <li>
        <button
          type="button"
          class="command-item ${index === selectedIndex ? 'selected' : ''}"
          data-index="${index}"
          style="width: 100%; padding: 0.5rem 0.75rem; display: flex; align-items: flex-start; gap: 0.75rem; text-align: left; background: transparent; border: none; cursor: pointer; color: var(--foreground); font-family: inherit; font-size: inherit;"
        >
          <div class="command-name-wrapper" style="flex-shrink: 0;">
            <span class="command-name" style="color: var(--primary); font-family: ui-monospace, 'Monaco', 'Menlo', monospace; font-size: 0.875rem;">${escapeHtml(command.display)}</span>
            ${command.argument_hint ? `<span class="command-hint" style="color: var(--muted-foreground); font-family: ui-monospace, 'Monaco', 'Menlo', monospace; font-size: 0.75rem; margin-left: 0.25rem;">${escapeHtml(command.argument_hint)}</span>` : ''}
          </div>
          <div class="command-description" style="flex: 1; min-width: 0;">
            <p style="font-size: 0.875rem; color: var(--foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0;">${escapeHtml(command.description)}</p>
          </div>
          <div class="command-type" style="flex-shrink: 0;">
            ${getTypeBadge(command.type)}
          </div>
        </button>
      </li>
    `).join('');

    portalContainer.innerHTML = `
      <div class="command-autocomplete-dropdown" style="position: fixed; z-index: 99999; pointer-events: auto; bottom: calc(100vh - ${dropdownPosition.top}px); left: ${dropdownPosition.left}px; width: ${dropdownPosition.width}px; background: var(--popover); border: 1px solid var(--border); border-radius: 0.5rem; box-shadow: 0 10px 40px -10px rgba(0, 0, 0, 0.3), 0 4px 20px -4px rgba(0, 0, 0, 0.2); max-height: 16rem; overflow: hidden; display: flex; flex-direction: column;">
        <div class="dropdown-header" style="padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); background: var(--muted); font-size: 0.75rem; color: var(--muted-foreground); flex-shrink: 0;">
          <span>${filteredCommands.length} command${filteredCommands.length !== 1 ? 's' : ''} available</span>
        </div>
        <ul class="dropdown-list" style="list-style: none; margin: 0; padding: 0; overflow-y: auto; max-height: 12rem; flex: 1; pointer-events: auto; overscroll-behavior: contain;">
          ${commandsHtml}
        </ul>
        <div class="dropdown-footer" style="padding: 0.375rem 0.75rem; border-top: 1px solid var(--border); background: var(--muted); font-size: 0.75rem; color: var(--muted-foreground); flex-shrink: 0;">
          <kbd style="display: inline-block; padding: 0.125rem 0.25rem; background: var(--secondary); border-radius: 0.25rem; margin: 0 0.125rem; font-family: inherit; font-size: 0.75rem;">Tab</kbd> or <kbd style="display: inline-block; padding: 0.125rem 0.25rem; background: var(--secondary); border-radius: 0.25rem; margin: 0 0.125rem; font-family: inherit; font-size: 0.75rem;">Enter</kbd> to select
          <kbd style="display: inline-block; padding: 0.125rem 0.25rem; background: var(--secondary); border-radius: 0.25rem; margin: 0 0.125rem; font-family: inherit; font-size: 0.75rem;">Esc</kbd> to close
        </div>
      </div>
    `;

    // Store reference to list element for scrolling
    listElement = portalContainer.querySelector('.dropdown-list');

    // Stop scroll events from propagating to prevent triggering updatePosition
    if (listElement) {
      listElement.addEventListener('scroll', (e) => {
        e.stopPropagation();
      }, { passive: true });

      // Also handle wheel events to prevent them from bubbling
      listElement.addEventListener('wheel', (e) => {
        e.stopPropagation();
      }, { passive: true });
    }

    // Add click handlers - capture current filteredCommands in closure
    const currentCommands = [...filteredCommands];
    
    portalContainer.querySelectorAll('.command-item').forEach((btn, i) => {
      
      btn.addEventListener('click', (e) => {
        
        e.preventDefault();
        e.stopPropagation();
        const index = parseInt((btn as HTMLElement).dataset.index || '0', 10);
        
        if (currentCommands[index]) {
          onSelect(currentCommands[index]);
        }
      });
      btn.addEventListener('mouseenter', () => {
        const index = parseInt((btn as HTMLElement).dataset.index || '0', 10);
        if (index !== selectedIndex) {
          // Update selected class without full re-render to avoid infinite loop/spam
          portalContainer?.querySelectorAll('.command-item').forEach((item, idx) => {
            item.classList.toggle('selected', idx === index);
          });
          selectedIndex = index;
        }
      });
    });
  }

  /**
   * Update selection styling without full re-render.
   * Used by keyboard navigation.
   */
  function updateSelectionStyling() {
    portalContainer?.querySelectorAll('.command-item').forEach((item, idx) => {
      item.classList.toggle('selected', idx === selectedIndex);
    });
  }

  function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function getTypeBadge(type: string): string {
    switch (type) {
      case 'plugin':
        return '<span class="type-badge type-plugin">plugin</span>';
      case 'interactive':
        return '<span class="type-badge type-interactive">interactive</span>';
      case 'sdk_builtin':
        return '<span class="type-badge type-builtin">builtin</span>';
      default:
        return '<span class="type-badge type-custom">custom</span>';
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
        updateSelectionStyling();
        scrollToSelected();
        return true;

      case 'ArrowUp':
        event.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, 0);
        updateSelectionStyling();
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

  onDestroy(() => {
    removePortal();
  });
</script>

<!-- Invisible anchor element to get position reference -->
<div bind:this={anchorElement} class="command-autocomplete-anchor"></div>

<style>
  /* Anchor element - invisible marker for position reference */
  .command-autocomplete-anchor {
    position: absolute;
    top: 0;
    left: 0;
    width: 0;
    height: 0;
    pointer-events: none;
    visibility: hidden;
  }

  /* Global styles for portal-rendered dropdown */
  :global(.command-autocomplete-portal-root) {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 99999;
    pointer-events: none;
  }

  :global(.command-autocomplete-dropdown) {
    position: fixed;
    z-index: 99999;
    pointer-events: auto;
    background: var(--popover);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    box-shadow: 0 10px 40px -10px rgba(0, 0, 0, 0.3), 0 4px 20px -4px rgba(0, 0, 0, 0.2);
    max-height: 16rem;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  :global(.command-autocomplete-dropdown .dropdown-header) {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
    background: var(--muted);
    font-size: 0.75rem;
    color: var(--muted-foreground);
    flex-shrink: 0;
  }

  :global(.command-autocomplete-dropdown .dropdown-list) {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    max-height: 12rem;
    flex: 1;
  }

  :global(.command-autocomplete-dropdown .command-item) {
    width: 100%;
    padding: 0.5rem 0.75rem;
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    text-align: left;
    background: transparent;
    border: none;
    cursor: pointer;
    transition: background-color 0.15s ease;
    color: var(--foreground);
    font-family: inherit;
    font-size: inherit;
  }

  :global(.command-autocomplete-dropdown .command-item:hover),
  :global(.command-autocomplete-dropdown .command-item.selected) {
    background: var(--accent);
  }

  :global(.command-autocomplete-dropdown .command-name-wrapper) {
    flex-shrink: 0;
  }

  :global(.command-autocomplete-dropdown .command-name) {
    color: var(--primary);
    font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
    font-size: 0.875rem;
  }

  :global(.command-autocomplete-dropdown .command-hint) {
    color: var(--muted-foreground);
    font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
    font-size: 0.75rem;
    margin-left: 0.25rem;
  }

  :global(.command-autocomplete-dropdown .command-description) {
    flex: 1;
    min-width: 0;
  }

  :global(.command-autocomplete-dropdown .command-description p) {
    font-size: 0.875rem;
    color: var(--foreground);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin: 0;
  }

  :global(.command-autocomplete-dropdown .command-type) {
    flex-shrink: 0;
  }

  :global(.command-autocomplete-dropdown .type-badge) {
    display: inline-block;
    padding: 0.125rem 0.375rem;
    font-size: 0.75rem;
    border-radius: 0.25rem;
  }

  :global(.command-autocomplete-dropdown .type-plugin) {
    background: rgba(168, 85, 247, 0.2);
    color: rgb(168, 85, 247);
  }

  :global(.command-autocomplete-dropdown .type-interactive) {
    background: color-mix(in srgb, var(--primary) 20%, transparent);
    color: var(--primary);
  }

  :global(.command-autocomplete-dropdown .type-builtin) {
    background: color-mix(in srgb, var(--success) 20%, transparent);
    color: var(--success);
  }

  :global(.command-autocomplete-dropdown .type-custom) {
    background: color-mix(in srgb, var(--info) 20%, transparent);
    color: var(--info);
  }

  :global(.command-autocomplete-dropdown .dropdown-footer) {
    padding: 0.375rem 0.75rem;
    border-top: 1px solid var(--border);
    background: var(--muted);
    font-size: 0.75rem;
    color: var(--muted-foreground);
    flex-shrink: 0;
  }

  :global(.command-autocomplete-dropdown .dropdown-footer kbd) {
    display: inline-block;
    padding: 0.125rem 0.25rem;
    background: var(--secondary);
    border-radius: 0.25rem;
    margin: 0 0.125rem;
    font-family: inherit;
    font-size: 0.75rem;
  }

  /* Scrollbar styling */
  :global(.command-autocomplete-dropdown .dropdown-list::-webkit-scrollbar) {
    width: 6px;
  }

  :global(.command-autocomplete-dropdown .dropdown-list::-webkit-scrollbar-track) {
    background: transparent;
  }

  :global(.command-autocomplete-dropdown .dropdown-list::-webkit-scrollbar-thumb) {
    background: var(--scrollbar-thumb, rgba(255, 255, 255, 0.1));
    border-radius: calc(var(--radius, 0.75rem) * 0.25);
  }

  :global(.command-autocomplete-dropdown .dropdown-list::-webkit-scrollbar-thumb:hover) {
    background: var(--scrollbar-thumb-hover, rgba(255, 255, 255, 0.2));
  }
</style>
