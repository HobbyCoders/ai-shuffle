<script lang="ts">
  import { getWorktreeFiles } from '$lib/api/git';
  import { getNotesAutocomplete } from '$lib/api/client';
  import './autocomplete/autocomplete-design-system.css';

  export interface FileItem {
    name: string;
    type: 'file' | 'directory' | 'chat_history' | 'note';
    path: string;
    size?: number | null;
    // Chat history specific fields
    title?: string | null;
    preview?: string | null;
    message_count?: number;
    modified_at?: string;
    // Note specific fields
    note_id?: string;
    note_tags?: string[];
  }

  // Virtual folder names
  const CHAT_HISTORY_FOLDER = 'Chat History';
  const NOTES_FOLDER = 'Notes';

  interface Props {
    inputValue: string;
    projectId?: string;
    worktreeId?: string;  // If provided, use worktree file API instead of project file API
    onSelect: (file: FileItem) => void;
    onClose: () => void;
    visible: boolean;
  }

  let { inputValue, projectId, worktreeId, onSelect, onClose, visible }: Props = $props();

  let files = $state<FileItem[]>([]);
  let filteredFiles = $state<FileItem[]>([]);
  let selectedIndex = $state(0);
  let loading = $state(false);
  let currentPath = $state('');
  let lastFetchedPath = $state<string | null>(null);
  let listElement = $state<HTMLUListElement | null>(null);
  let inChatHistoryFolder = $state(false);
  let inNotesFolder = $state(false);

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

    // Sort: Virtual folders first, then directories, then by match relevance
    filteredFiles = filtered.sort((a, b) => {
      // Chat History folder always first
      if (a.name === CHAT_HISTORY_FOLDER && b.name !== CHAT_HISTORY_FOLDER) return -1;
      if (b.name === CHAT_HISTORY_FOLDER && a.name !== CHAT_HISTORY_FOLDER) return 1;

      // Notes folder second
      if (a.name === NOTES_FOLDER && b.name !== NOTES_FOLDER) return -1;
      if (b.name === NOTES_FOLDER && a.name !== NOTES_FOLDER) return 1;

      // Directories before files (but not for chat_history/note items which have their own order)
      if (a.type === 'directory' && b.type !== 'directory' && b.type !== 'chat_history' && b.type !== 'note') return -1;
      if (b.type === 'directory' && a.type !== 'directory' && a.type !== 'chat_history' && a.type !== 'note') return 1;

      // Chat history and note items: preserve backend order (most recent first by updated_at)
      // Don't re-sort them - the backend already sorted by date
      if ((a.type === 'chat_history' && b.type === 'chat_history') ||
          (a.type === 'note' && b.type === 'note')) {
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

  /**
   * Check if the path indicates we're in the Notes virtual folder
   */
  function isNotesPath(path: string): boolean {
    return path === NOTES_FOLDER || path.startsWith(NOTES_FOLDER + '/');
  }

  /**
   * Get the search filter from a Notes path
   */
  function getNotesFilter(path: string): string {
    if (path === NOTES_FOLDER) return '';
    return path.slice(NOTES_FOLDER.length + 1);
  }

  async function fetchFiles(path: string) {
    if (!projectId) return;

    loading = true;
    lastFetchedPath = path;
    currentPath = path;

    // Check if we're in the Chat History virtual folder
    if (isChatHistoryPath(path)) {
      inChatHistoryFolder = true;
      inNotesFolder = false;
      await fetchChatHistory(getChatHistoryFilter(path));
      return;
    }

    // Check if we're in the Notes virtual folder
    if (isNotesPath(path)) {
      inNotesFolder = true;
      inChatHistoryFolder = false;
      await fetchNotes(getNotesFilter(path));
      return;
    }

    inChatHistoryFolder = false;
    inNotesFolder = false;

    try {
      // If we have a worktreeId, use the worktree file API
      // This allows browsing files in the worktree's directory instead of the main project
      if (worktreeId) {
        const response = await getWorktreeFiles(projectId, worktreeId, path);
        // Add virtual folders at root level (for worktrees too)
        if (!path) {
          files = [
            {
              name: CHAT_HISTORY_FOLDER,
              type: 'directory' as const,
              path: CHAT_HISTORY_FOLDER,
              size: null
            },
            {
              name: NOTES_FOLDER,
              type: 'directory' as const,
              path: NOTES_FOLDER,
              size: null
            },
            ...response.files.map(f => ({
              name: f.name,
              type: f.type as 'file' | 'directory',
              path: f.path,
              size: f.size
            }))
          ];
        } else {
          files = response.files.map(f => ({
            name: f.name,
            type: f.type as 'file' | 'directory',
            path: f.path,
            size: f.size
          }));
        }
      } else {
        // Use the standard project file API
        const params = new URLSearchParams();
        if (path) {
          params.set('path', path);
        }

        const response = await fetch(`/api/v1/projects/${projectId}/files?${params}`, {
          credentials: 'include'
        });

        if (response.ok) {
          const data = await response.json();
          // Add virtual folders at root level
          if (!path) {
            files = [
              {
                name: CHAT_HISTORY_FOLDER,
                type: 'directory' as const,
                path: CHAT_HISTORY_FOLDER,
                size: null
              },
              {
                name: NOTES_FOLDER,
                type: 'directory' as const,
                path: NOTES_FOLDER,
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

  async function fetchNotes(search: string = '') {
    try {
      const notes = await getNotesAutocomplete(search || undefined, 30);
      files = notes.map((note) => ({
        name: note.title,
        type: 'note' as const,
        path: `${NOTES_FOLDER}/${note.id}`,
        note_id: note.id,
        preview: note.preview,
        note_tags: note.tags
      }));
    } catch (error) {
      console.error('Failed to fetch notes:', error);
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

      case 'Backspace':
        // If the input ends with a path (trailing /), go back to parent directory
        const backspaceAtInfo = extractAtQuery(inputValue);
        if (backspaceAtInfo && backspaceAtInfo.query.endsWith('/')) {
          event.preventDefault();
          // Get the parent path - remove trailing slash and last path component
          const pathWithoutTrailingSlash = backspaceAtInfo.query.slice(0, -1);
          const lastSlashIdx = pathWithoutTrailingSlash.lastIndexOf('/');
          if (lastSlashIdx === -1) {
            // At root level - use a special marker to tell parent to just show @
            // We use type 'directory' with empty path, parent will handle it
            onSelect({ name: '', type: 'directory', path: '' });
          } else {
            // Go to parent directory (keep trailing slash)
            const parentPath = pathWithoutTrailingSlash.slice(0, lastSlashIdx + 1);
            onSelect({ name: '', type: 'directory', path: parentPath });
          }
          return true;
        }
        return false;

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
    inNotesFolder = false;
    fetchFiles('');
  }

  function goToChatHistory() {
    fetchFiles(CHAT_HISTORY_FOLDER);
  }

  function goToNotes() {
    fetchFiles(NOTES_FOLDER);
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
      return `${diffDays}d ago`;
    } else if (diffDays < 30) {
      const weeks = Math.floor(diffDays / 7);
      return `${weeks}w ago`;
    } else {
      return date.toLocaleDateString();
    }
  }

  function getIconClass(file: FileItem): string {
    if (file.type === 'directory') {
      if (file.name === CHAT_HISTORY_FOLDER) return 'autocomplete-icon-history';
      if (file.name === NOTES_FOLDER) return 'autocomplete-icon-notes';
      return 'autocomplete-icon-folder';
    }
    if (file.type === 'chat_history') return 'autocomplete-icon-history';
    if (file.type === 'note') return 'autocomplete-icon-notes';
    return 'autocomplete-icon-file';
  }

  function getBadgeClass(file: FileItem): string {
    if (file.type === 'directory') {
      if (file.name === CHAT_HISTORY_FOLDER) return 'type-history';
      if (file.name === NOTES_FOLDER) return 'type-notes';
      return 'type-folder';
    }
    if (file.type === 'chat_history') return 'type-chat';
    if (file.type === 'note') return 'type-note';
    return '';
  }

  function getBadgeText(file: FileItem): string {
    if (file.type === 'directory') {
      if (file.name === CHAT_HISTORY_FOLDER) return 'history';
      if (file.name === NOTES_FOLDER) return 'notes';
      return 'folder';
    }
    if (file.type === 'chat_history') return 'chat';
    if (file.type === 'note') return 'note';
    return '';
  }
</script>

{#if visible && (filteredFiles.length > 0 || loading)}
  <div class="autocomplete-panel file-autocomplete-panel">
    <!-- Header -->
    <div class="autocomplete-header">
      <span class="autocomplete-count">
        {#if loading}
          {inChatHistoryFolder ? 'Loading history...' : inNotesFolder ? 'Loading notes...' : 'Loading...'}
        {:else if inChatHistoryFolder}
          {filteredFiles.length} chat{filteredFiles.length !== 1 ? 's' : ''}
        {:else if inNotesFolder}
          {filteredFiles.length} note{filteredFiles.length !== 1 ? 's' : ''}
        {:else}
          {filteredFiles.length} item{filteredFiles.length !== 1 ? 's' : ''}
          {#if currentPath}
            <span class="file-path-indicator">/{currentPath}</span>
          {/if}
        {/if}
      </span>
      {#if currentPath}
        <button
          type="button"
          class="autocomplete-back-btn"
          onclick={goToRoot}
        >
          ← root
        </button>
      {/if}
    </div>

    <!-- Loading state -->
    {#if loading}
      <div class="autocomplete-loading">
        <div class="autocomplete-loading-spinner"></div>
        <span>Loading...</span>
      </div>
    {:else}
      <!-- File list -->
      <ul bind:this={listElement} class="autocomplete-list file-autocomplete-list">
        {#each filteredFiles as file, index}
          <li>
            <button
              type="button"
              class="autocomplete-item"
              class:selected={index === selectedIndex}
              onclick={() => handleSelect(file)}
              onmouseenter={() => selectedIndex = index}
            >
              <!-- Icon -->
              <div class="autocomplete-item-icon {getIconClass(file)}">
                {#if file.type === 'directory'}
                  {#if file.name === CHAT_HISTORY_FOLDER}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                  {:else if file.name === NOTES_FOLDER}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  {:else}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                    </svg>
                  {/if}
                {:else if file.type === 'chat_history'}
                  <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                {:else if file.type === 'note'}
                  <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                {:else}
                  <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                {/if}
              </div>

              <!-- Content -->
              <div class="autocomplete-item-content">
                <span class="autocomplete-item-name">
                  {file.name}{file.type === 'directory' ? '/' : ''}
                </span>
                {#if (file.type === 'chat_history' || file.type === 'note') && file.preview}
                  <span class="autocomplete-item-description">
                    {file.preview}
                  </span>
                {/if}
              </div>

              <!-- Meta info -->
              {#if file.type === 'file' && file.size}
                <span class="autocomplete-item-meta">
                  {formatSize(file.size)}
                </span>
              {:else if file.type === 'chat_history'}
                <span class="autocomplete-item-meta">
                  {file.message_count} msgs{file.modified_at ? ` · ${formatRelativeDate(file.modified_at)}` : ''}
                </span>
              {:else if file.type === 'note' && file.note_tags && file.note_tags.length > 0}
                <span class="autocomplete-item-meta file-note-tags">
                  {file.note_tags.slice(0, 2).join(', ')}{file.note_tags.length > 2 ? '...' : ''}
                </span>
              {/if}

              <!-- Type badge -->
              {#if getBadgeText(file)}
                <span class="autocomplete-badge {getBadgeClass(file)}">
                  {getBadgeText(file)}
                </span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <!-- Footer -->
    <div class="autocomplete-footer">
      <span><kbd class="autocomplete-kbd">Tab</kbd> <span class="autocomplete-hint-text">nav/select</span></span>
      <span><kbd class="autocomplete-kbd">Enter</kbd> <span class="autocomplete-hint-text">select</span></span>
      <span><kbd class="autocomplete-kbd">Esc</kbd> <span class="autocomplete-hint-text">close</span></span>
    </div>
  </div>
{/if}

<style>
  /* FileAutocomplete-specific positioning and sizing */
  .file-autocomplete-panel {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: 8px;
    max-height: 20rem;
    z-index: 50;

    /* Solid background since we're not portaled to body like CommandAutocomplete */
    background: var(--card);
  }

  .file-autocomplete-list {
    max-height: 14rem;
  }

  .file-path-indicator {
    opacity: 0.6;
    font-family: var(--card-font-mono, 'JetBrains Mono', ui-monospace, monospace);
    margin-left: 4px;
  }

  .file-note-tags {
    max-width: 6rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
