<script lang="ts">
  export interface FileItem {
    name: string;
    type: 'file' | 'directory';
    path: string;
    size?: number | null;
  }

  interface Props {
    inputValue: string;
    projectId?: string;
    onSelect: (file: FileItem) => void;
    onClose: () => void;
    visible: boolean;
  }

  let { inputValue, projectId, onSelect, onClose, visible }: Props = $props();

  let files = $state<FileItem[]>([]);
  let filteredFiles = $state<FileItem[]>([]);
  let selectedIndex = $state(0);
  let loading = $state(false);
  let currentPath = $state('');
  let lastFetchedPath = $state<string | null>(null);
  let listElement: HTMLUListElement;

  // Extract the @ query from input - finds the last @ and text after it
  // Supports paths with spaces by capturing everything after @ until end of input
  function extractAtQuery(input: string): { query: string; atIndex: number } | null {
    // Find the last @ that's either at the start or preceded by whitespace
    // Search backwards to find the last valid @ position
    let atIndex = -1;
    for (let i = input.length - 1; i >= 0; i--) {
      if (input[i] === '@') {
        // Check if @ is at start or preceded by whitespace
        if (i === 0 || /\s/.test(input[i - 1])) {
          atIndex = i;
          break;
        }
      }
    }

    if (atIndex === -1) return null;

    // Everything after @ is the query
    const query = input.slice(atIndex + 1);

    return { query, atIndex };
  }

  // Get the directory path from query
  function getPathFromQuery(query: string): string {
    if (!query.includes('/')) return '';
    const parts = query.split('/');
    return parts.slice(0, -1).join('/');
  }

  // Get the filename filter from query
  function getFilterFromQuery(query: string): string {
    if (!query.includes('/')) return query;
    return query.split('/').pop() || '';
  }

  // Fetch files when visible and projectId changes - initial load
  $effect(() => {
    if (visible && projectId && lastFetchedPath === null) {
      fetchFiles('');
    }
  });

  // Handle path changes from query - separate effect to avoid loops
  $effect(() => {
    if (!visible || !projectId) return;

    const atInfo = extractAtQuery(inputValue);
    if (!atInfo) return;

    // Don't lowercase the path - file systems can be case-sensitive
    const query = atInfo.query;
    const targetPath = getPathFromQuery(query);

    // Only fetch if path actually changed and we haven't fetched this path yet
    if (targetPath !== currentPath && targetPath !== lastFetchedPath) {
      fetchFiles(targetPath);
    }
  });

  // Filter files based on @ query - pure filtering, no fetching
  $effect(() => {
    const atInfo = extractAtQuery(inputValue);
    if (!atInfo) {
      filteredFiles = [];
      return;
    }

    // Keep original query for path, but use lowercase for filtering
    const query = atInfo.query;
    const filterPart = getFilterFromQuery(query).toLowerCase();

    // Filter files by name
    let filtered: FileItem[];
    if (!filterPart) {
      filtered = [...files];
    } else {
      filtered = files.filter(f =>
        f.name.toLowerCase().includes(filterPart)
      );
    }

    // Sort: directories first, then by match relevance
    filteredFiles = filtered.sort((a, b) => {
      // Directories first
      if (a.type === 'directory' && b.type !== 'directory') return -1;
      if (b.type === 'directory' && a.type !== 'directory') return 1;

      const aName = a.name.toLowerCase();
      const bName = b.name.toLowerCase();

      // Exact match first
      if (aName === filterPart && bName !== filterPart) return -1;
      if (bName === filterPart && aName !== filterPart) return 1;

      // Prefix match second
      const aStarts = aName.startsWith(filterPart);
      const bStarts = bName.startsWith(filterPart);
      if (aStarts && !bStarts) return -1;
      if (bStarts && !aStarts) return 1;

      // Alphabetical
      return aName.localeCompare(bName);
    });

    selectedIndex = 0;
  });

  // Reset when becoming invisible
  $effect(() => {
    if (!visible) {
      lastFetchedPath = null;
      currentPath = '';
      files = [];
      filteredFiles = [];
    }
  });

  async function fetchFiles(path: string) {
    if (!projectId) return;

    loading = true;
    lastFetchedPath = path;
    currentPath = path;

    try {
      const params = new URLSearchParams();
      if (path) {
        params.set('path', path);
      }

      const response = await fetch(`/api/v1/projects/${projectId}/files?${params}`, {
        credentials: 'include'
      });

      if (response.ok) {
        const data = await response.json();
        files = data.files;
      } else {
        files = [];
      }
    } catch (error) {
      console.error('Failed to fetch files:', error);
      files = [];
    } finally {
      loading = false;
    }
  }

  /**
   * Handle keyboard events for the autocomplete.
   * Returns true if the event was handled.
   */
  export function handleKeyDown(event: KeyboardEvent): boolean {
    if (!visible || filteredFiles.length === 0) return false;

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        selectedIndex = Math.min(selectedIndex + 1, filteredFiles.length - 1);
        scrollToSelected();
        return true;

      case 'ArrowUp':
        event.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, 0);
        scrollToSelected();
        return true;

      case 'Tab':
        event.preventDefault();
        event.stopPropagation();
        const selected = filteredFiles[selectedIndex];
        if (selected) {
          if (selected.type === 'directory') {
            // For directories, navigate into them
            navigateToDirectory(selected);
          } else {
            // For files, select them
            onSelect(selected);
          }
        }
        return true;

      case 'Enter':
        if (filteredFiles[selectedIndex]) {
          event.preventDefault();
          event.stopPropagation();
          onSelect(filteredFiles[selectedIndex]);
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

  function navigateToDirectory(dir: FileItem) {
    // This will be handled by updating the input in the parent
    // For now, we'll select the directory which should update the path
    onSelect({ ...dir, path: dir.path + '/' });
  }

  function scrollToSelected() {
    if (listElement) {
      const selected = listElement.children[selectedIndex] as HTMLElement;
      if (selected) {
        selected.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function handleSelect(file: FileItem) {
    onSelect(file);
  }

  function formatSize(size: number | null | undefined): string {
    if (size === null || size === undefined) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function goToRoot() {
    fetchFiles('');
  }
</script>

{#if visible && (filteredFiles.length > 0 || loading)}
  <div class="absolute bottom-full left-0 right-0 mb-2 bg-gray-800 border border-gray-700 rounded-lg shadow-xl max-h-64 overflow-hidden z-50">
    <div class="px-3 py-2 border-b border-gray-700 bg-gray-750 flex items-center justify-between">
      <span class="text-xs text-gray-400">
        {#if loading}
          Loading files...
        {:else}
          {filteredFiles.length} file{filteredFiles.length !== 1 ? 's' : ''}
          {#if currentPath}
            in /{currentPath}
          {/if}
        {/if}
      </span>
      {#if currentPath}
        <button
          type="button"
          class="text-xs text-blue-400 hover:text-blue-300"
          onclick={goToRoot}
        >
          ← root
        </button>
      {/if}
    </div>

    {#if !loading}
      <ul bind:this={listElement} class="overflow-y-auto max-h-48">
        {#each filteredFiles as file, index}
          <li>
            <button
              type="button"
              class="w-full px-3 py-2 flex items-center gap-3 text-left hover:bg-gray-700 transition-colors {index === selectedIndex ? 'bg-gray-700' : ''}"
              onclick={() => handleSelect(file)}
              onmouseenter={() => selectedIndex = index}
            >
              <!-- Icon -->
              <div class="flex-shrink-0">
                {#if file.type === 'directory'}
                  <svg class="w-4 h-4 text-yellow-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                  </svg>
                {:else}
                  <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                {/if}
              </div>

              <!-- Name -->
              <div class="flex-1 min-w-0">
                <span class="text-sm text-gray-200 truncate block">
                  {file.name}{file.type === 'directory' ? '/' : ''}
                </span>
              </div>

              <!-- Size (for files) -->
              {#if file.type === 'file' && file.size}
                <span class="text-xs text-gray-500 flex-shrink-0">
                  {formatSize(file.size)}
                </span>
              {/if}

              <!-- Type badge -->
              <div class="flex-shrink-0">
                {#if file.type === 'directory'}
                  <span class="px-1.5 py-0.5 text-xs bg-yellow-900/50 text-yellow-300 rounded">
                    folder
                  </span>
                {/if}
              </div>
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="px-3 py-1.5 border-t border-gray-700 bg-gray-750 text-xs text-gray-500">
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mr-1">Tab</kbd> navigate/select
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mx-1">Enter</kbd> select
      <kbd class="px-1 py-0.5 bg-gray-700 rounded mx-1">Esc</kbd> close
    </div>
  </div>
{/if}

<style>
  .bg-gray-750 {
    background-color: rgb(42, 42, 54);
  }
</style>
