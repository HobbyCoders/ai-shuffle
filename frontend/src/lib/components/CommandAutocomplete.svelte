<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import './autocomplete/autocomplete-design-system.css';

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
    const dropdown = portalContainer?.querySelector('.autocomplete-panel') as HTMLElement;
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
    portalContainer.className = 'autocomplete-portal-root';
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
          class="autocomplete-item ${index === selectedIndex ? 'selected' : ''}"
          data-index="${index}"
        >
          <div class="autocomplete-item-content">
            <div class="autocomplete-item-name">
              <span class="command-name">${escapeHtml(command.display)}</span>
              ${command.argument_hint ? `<span class="command-hint">${escapeHtml(command.argument_hint)}</span>` : ''}
            </div>
            <div class="autocomplete-item-description">${escapeHtml(command.description)}</div>
          </div>
          <span class="autocomplete-badge type-${command.type === 'sdk_builtin' ? 'builtin' : command.type}">${getTypeName(command.type)}</span>
        </button>
      </li>
    `).join('');

    portalContainer.innerHTML = `
      <div class="autocomplete-panel" style="position: fixed; bottom: calc(100vh - ${dropdownPosition.top}px); left: ${dropdownPosition.left}px; width: ${dropdownPosition.width}px; max-height: 20rem;">
        <div class="autocomplete-header">
          <span class="autocomplete-count">${filteredCommands.length} command${filteredCommands.length !== 1 ? 's' : ''}</span>
          <span class="autocomplete-hint-text">Type to filter</span>
        </div>
        <ul class="autocomplete-list" style="max-height: 14rem;">
          ${commandsHtml}
        </ul>
        <div class="autocomplete-footer">
          <span><kbd class="autocomplete-kbd">↑↓</kbd> <span class="autocomplete-hint-text">navigate</span></span>
          <span><kbd class="autocomplete-kbd">Tab</kbd> <span class="autocomplete-hint-text">select</span></span>
          <span><kbd class="autocomplete-kbd">Esc</kbd> <span class="autocomplete-hint-text">close</span></span>
        </div>
      </div>
    `;

    // Store reference to list element for scrolling
    listElement = portalContainer.querySelector('.autocomplete-list');

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

    portalContainer.querySelectorAll('.autocomplete-item').forEach((btn, i) => {

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
          portalContainer?.querySelectorAll('.autocomplete-item').forEach((item, idx) => {
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
    portalContainer?.querySelectorAll('.autocomplete-item').forEach((item, idx) => {
      item.classList.toggle('selected', idx === selectedIndex);
    });
  }

  function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function getTypeName(type: string): string {
    switch (type) {
      case 'plugin':
        return 'plugin';
      case 'interactive':
        return 'interactive';
      case 'sdk_builtin':
        return 'builtin';
      default:
        return 'custom';
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
</style>
