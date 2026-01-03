<script lang="ts">
  export interface FileItem {
    name: string;
    type: 'file' | 'directory' | 'chat_history';
    path: string;
    size?: number | null;
    // Chat history specific fields
    title?: string | null;
    preview?: string | null;
    message_count?: number;
    modified_at?: string;
  }

  // Virtual folder name for chat history
  const CHAT_HISTORY_FOLDER = 'Chat History';

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
  let listElement = $state<HTMLUListElement | null>(null);
  let inChatHistoryFolder = $state(false);

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
  // Handles paths with spaces like "Chat History/"
  function getPathFromQuery(query: string): string {
    if (!query.includes('/')) return '';
    // Find the last slash position
    const lastSlashIndex = query.lastIndexOf('/');
    return query.slice(0, lastSlashIndex);
  }

  // Get the filename filter from query (text after the last /)
  function getFilterFromQuery(query: string): string {
    if (!query.includes('/')) return query;
    const lastSlashIndex = query.lastIndexOf('/');
    return query.slice(lastSlashIndex + 1);
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

    // Sort: Chat History first, then directories, then by match relevance
    filteredFiles = filtered.sort((a, b) => {
      // Chat History folder always first
      if (a.name === CHAT_HISTORY_FOLDER && b.name !== CHAT_HISTORY_FOLDER) return -1;
      if (b.name === CHAT_HISTORY_FOLDER && a.name !== CHAT_HISTORY_FOLDER) return 1;

      // Directories before files (but not for chat_history items which have their own order)
      if (a.type === 'directory' && b.type !== 'directory' && b.type !== 'chat_history') return -1;
      if (b.type === 'directory' && a.type !== 'directory' && a.type !== 'chat_history') return 1;

      // Chat history items: preserve backend order (most recent first by modified_at)
      // Don't re-sort them - the backend already sorted by date
      if (a.type === 'chat_history' && b.type === 'chat_history') {
        return 0; // Preserve original order from backend
      }

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

      // Alphabetical for files/directories
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

  /**
   * Check if the path indicates we're in the Chat History virtual folder
   */
  function isChatHistoryPath(path: string): boolean {
    return path === CHAT_HISTORY_FOLDER || path.startsWith(CHAT_HISTORY_FOLDER + '/');
  }

  /**
   * Get the search filter from a Chat History path
   */
  function getChatHistoryFilter(path: string): string {
    if (path === CHAT_HISTORY_FOLDER) return '';
    return path.slice(CHAT_HISTORY_FOLDER.length + 1);
  }

  async function fetchFiles(path: string) {
    if (!projectId) return;

    loading = true;
    lastFetchedPath = path;
    currentPath = path;

    // Check if we're in the Chat History virtual folder
    if (isChatHistoryPath(path)) {
      inChatHistoryFolder = true;
      await fetchChatHistory(getChatHistoryFilter(path));
      return;
    }

    inChatHistoryFolder = false;

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
        // Add Chat History virtual folder at root level
        if (!path) {
          files = [
            {
              name: CHAT_HISTORY_FOLDER,
              type: 'directory' as const,
              path: CHAT_HISTORY_FOLDER,
              size: null
            },
            ...data.files
          ];
        } else {
          files = data.files;
        }
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

  async function fetchChatHistory(search: string = '') {
    if (!projectId) return;

    try {
      const params = new URLSearchParams();
      if (search) {
        params.set('search', search);
      }
      params.set('limit', '30');

      const response = await fetch(`/api/v1/projects/${projectId}/chat-history?${params}`, {
        credentials: 'include'
      });

      if (response.ok) {
        const data = await response.json();
        files = data.sessions.map((session: any) => ({
          name: session.name,
          type: 'chat_history' as const,
          path: session.path,
          size: session.size_bytes,
          title: session.title,
          preview: session.preview,
          message_count: session.message_count,
          modified_at: session.modified_at
        }));
      } else {
        files = [];
      }
    } catch (error) {
      console.error('Failed to fetch chat history:', error);
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
    inChatHistoryFolder = false;
    fetchFiles('');
  }

  function goToChatHistory() {
    fetchFiles(CHAT_HISTORY_FOLDER);
  }

  function formatRelativeDate(isoDate: string): string {
    const date = new Date(isoDate);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return 'Today';
    } else if (diffDays === 1) {
      return 'Yesterday';
    } else if (diffDays < 7) {
      return `${diffDays} days ago`;
    } else if (diffDays < 30) {
      const weeks = Math.floor(diffDays / 7);
      return `${weeks} week${weeks > 1 ? 's' : ''} ago`;
    } else {
      return date.toLocaleDateString();
    }
  }
</script>

{#if visible && (filteredFiles.length > 0 || loading)}
  <div class="absolute bottom-full left-0 right-0 mb-2 bg-popover border border-border rounded-lg shadow-xl max-h-64 overflow-hidden z-50">
    <div class="px-3 py-2 border-b border-border bg-muted flex items-center justify-between">
      <span class="text-xs text-muted-foreground">
        {#if loading}
          {inChatHistoryFolder ? 'Loading chat history...' : 'Loading files...'}
        {:else if inChatHistoryFolder}
          {filteredFiles.length} chat{filteredFiles.length !== 1 ? 's' : ''} in Chat History
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
          class="text-xs text-primary hover:opacity-80"
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
              class="w-full px-3 py-2 flex items-center gap-3 text-left hover:bg-accent transition-colors {index === selectedIndex ? 'bg-accent' : ''}"
              onclick={() => handleSelect(file)}
              onmouseenter={() => selectedIndex = index}
            >
              <!-- Icon -->
              <div class="flex-shrink-0">
                {#if file.type === 'directory'}
                  <!-- Special icon for Chat History folder -->
                  {#if file.name === CHAT_HISTORY_FOLDER}
                    <svg class="w-4 h-4 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                  {:else}
                    <svg class="w-4 h-4 text-warning" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                    </svg>
                  {/if}
                {:else if file.type === 'chat_history'}
                  <svg class="w-4 h-4 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                {:else}
                  <svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                {/if}
              </div>

              <!-- Name and details -->
              <div class="flex-1 min-w-0">
                {#if file.type === 'chat_history'}
                  <span class="text-sm text-foreground truncate block">
                    {file.name}
                  </span>
                  {#if file.preview}
                    <span class="text-xs text-muted-foreground truncate block">
                      {file.preview}
                    </span>
                  {/if}
                {:else}
                  <span class="text-sm text-foreground truncate block">
                    {file.name}{file.type === 'directory' ? '/' : ''}
                  </span>
                {/if}
              </div>

              <!-- Size/count info -->
              {#if file.type === 'file' && file.size}
                <span class="text-xs text-muted-foreground flex-shrink-0">
                  {formatSize(file.size)}
                </span>
              {:else if file.type === 'chat_history'}
                <span class="text-xs text-muted-foreground flex-shrink-0">
                  {file.message_count} msgs · {file.modified_at ? formatRelativeDate(file.modified_at) : ''}
                </span>
              {/if}

              <!-- Type badge -->
              <div class="flex-shrink-0">
                {#if file.type === 'directory'}
                  {#if file.name === CHAT_HISTORY_FOLDER}
                    <span class="px-1.5 py-0.5 text-xs bg-purple-500/20 text-purple-400 rounded">
                      history
                    </span>
                  {:else}
                    <span class="px-1.5 py-0.5 text-xs bg-warning/20 text-warning rounded">
                      folder
                    </span>
                  {/if}
                {:else if file.type === 'chat_history'}
                  <span class="px-1.5 py-0.5 text-xs bg-purple-500/20 text-purple-400 rounded">
                    chat
                  </span>
                {/if}
              </div>
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="px-3 py-1.5 border-t border-border bg-muted text-xs text-muted-foreground">
      <kbd class="px-1 py-0.5 bg-secondary rounded mr-1">Tab</kbd> navigate/select
      <kbd class="px-1 py-0.5 bg-secondary rounded mx-1">Enter</kbd> select
      <kbd class="px-1 py-0.5 bg-secondary rounded mx-1">Esc</kbd> close
    </div>
  </div>
{/if}
