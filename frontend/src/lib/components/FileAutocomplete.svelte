<script lang="ts">
  import { getWorktreeFiles } from '$lib/api/git';
  import { apiBase, withAuth } from '$lib/api/runtime';
  import { getNotesAutocomplete, getSubagentsForAutocomplete } from '$lib/api/client';
  import { extractAtMentionQuery } from '$lib/components/chat/tiptap/serialization';
  import './autocomplete/autocomplete-design-system.css';

  export interface FileItem {
    name: string;
    type: 'file' | 'directory' | 'chat_history' | 'note' | 'subagent';
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
    // Subagent specific fields
    subagent_id?: string;
    subagent_description?: string;
    subagent_source?: 'profile' | 'plugin' | 'project' | 'user';
    subagent_prompt?: string;
    subagent_tools?: string[];
    subagent_model?: string | null;
  }

  // Virtual folder names
  const CHAT_HISTORY_FOLDER = 'Chat History';
  const NOTES_FOLDER = 'Notes';
  const SUBAGENTS_FOLDER = 'Subagents';
  const VIRTUAL_FOLDER_NAMES = [CHAT_HISTORY_FOLDER, NOTES_FOLDER, SUBAGENTS_FOLDER];

  interface Props {
    inputValue: string;
    projectId?: string;
    profileId?: string;
    worktreeId?: string;
    sessionId?: string;
    onSelect: (file: FileItem) => void;
    onClose: () => void;
    visible: boolean;
  }

  let { inputValue, projectId, profileId, worktreeId, sessionId, onSelect, onClose, visible }: Props = $props();

  let files = $state<FileItem[]>([]);
  let filteredFiles = $state<FileItem[]>([]);
  let selectedIndex = $state(0);
  let loading = $state(false);
  let currentPath = $state('');
  let lastFetchedPath = $state<string | null>(null);
  let listElement = $state<HTMLUListElement | null>(null);
  let inChatHistoryFolder = $state(false);
  let inNotesFolder = $state(false);
  let inSubagentsFolder = $state(false);
  let fetchCounter = 0;

  // Get the directory path from query
  function getPathFromQuery(query: string): string {
    if (!query.includes('/')) return '';
    const lastSlashIndex = query.lastIndexOf('/');
    return query.slice(0, lastSlashIndex);
  }

  // Get the filename filter from query (text after the last /)
  function getFilterFromQuery(query: string): string {
    if (!query.includes('/')) return query;
    const lastSlashIndex = query.lastIndexOf('/');
    return query.slice(lastSlashIndex + 1);
  }

  // Current filter query for match highlighting
  let currentFilterQuery = $derived.by(() => {
    const atInfo = extractAtMentionQuery(inputValue);
    if (!atInfo) return '';
    return getFilterFromQuery(atInfo.query);
  });

  // Section grouping: virtual folders vs regular files (root level only)
  let isAtRoot = $derived(!currentPath && !inChatHistoryFolder && !inNotesFolder && !inSubagentsFolder);
  let quickAccessItems = $derived(
    isAtRoot ? filteredFiles.filter(f => VIRTUAL_FOLDER_NAMES.includes(f.name)) : []
  );
  let fileItems = $derived(
    isAtRoot ? filteredFiles.filter(f => !VIRTUAL_FOLDER_NAMES.includes(f.name)) : filteredFiles
  );
  let showSectionHeaders = $derived(quickAccessItems.length > 0 && fileItems.length > 0);

  // Breadcrumb segments from current path
  let breadcrumbSegments = $derived.by(() => {
    if (!currentPath) return [];
    const parts = currentPath.split('/').filter(Boolean);
    return parts.map((part, i) => ({
      name: part,
      path: parts.slice(0, i + 1).join('/')
    }));
  });

  // Highlight matching text in a name
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

  // Track previous visibility to detect transitions
  let wasVisible = $state(false);

  // Handle visibility changes and initial load
  $effect(() => {
    if (!visible && wasVisible) {
      lastFetchedPath = null;
      currentPath = '';
      files = [];
      filteredFiles = [];
      inChatHistoryFolder = false;
      inNotesFolder = false;
      inSubagentsFolder = false;
    } else if (visible && !wasVisible && projectId) {
      fetchFiles('');
    }
    wasVisible = visible;
  });

  // Handle path changes from query
  $effect(() => {
    if (!visible || !projectId) return;

    const atInfo = extractAtMentionQuery(inputValue);
    if (!atInfo) return;

    const query = atInfo.query;
    const targetPath = getPathFromQuery(query);

    if (targetPath !== currentPath && targetPath !== lastFetchedPath) {
      fetchFiles(targetPath);
    }
  });

  // Filter files based on @ query
  $effect(() => {
    const atInfo = extractAtMentionQuery(inputValue);
    if (!atInfo) {
      filteredFiles = [];
      return;
    }

    const query = atInfo.query;
    const filterPart = getFilterFromQuery(query).toLowerCase();

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
      if (a.name === CHAT_HISTORY_FOLDER && b.name !== CHAT_HISTORY_FOLDER) return -1;
      if (b.name === CHAT_HISTORY_FOLDER && a.name !== CHAT_HISTORY_FOLDER) return 1;
      if (a.name === NOTES_FOLDER && b.name !== NOTES_FOLDER) return -1;
      if (b.name === NOTES_FOLDER && a.name !== NOTES_FOLDER) return 1;
      if (a.name === SUBAGENTS_FOLDER && b.name !== SUBAGENTS_FOLDER) return -1;
      if (b.name === SUBAGENTS_FOLDER && a.name !== SUBAGENTS_FOLDER) return 1;

      if (a.type === 'directory' && b.type !== 'directory' && b.type !== 'chat_history' && b.type !== 'note' && b.type !== 'subagent') return -1;
      if (b.type === 'directory' && a.type !== 'directory' && a.type !== 'chat_history' && a.type !== 'note' && a.type !== 'subagent') return 1;

      if ((a.type === 'chat_history' && b.type === 'chat_history') ||
          (a.type === 'note' && b.type === 'note')) {
        return 0;
      }

      const aName = a.name.toLowerCase();
      const bName = b.name.toLowerCase();

      if (aName === filterPart && bName !== filterPart) return -1;
      if (bName === filterPart && aName !== filterPart) return 1;

      const aStarts = aName.startsWith(filterPart);
      const bStarts = bName.startsWith(filterPart);
      if (aStarts && !bStarts) return -1;
      if (bStarts && !aStarts) return 1;

      return aName.localeCompare(bName);
    });

    selectedIndex = 0;
  });

  function isChatHistoryPath(path: string): boolean {
    return path === CHAT_HISTORY_FOLDER || path.startsWith(CHAT_HISTORY_FOLDER + '/');
  }

  function getChatHistoryFilter(path: string): string {
    if (path === CHAT_HISTORY_FOLDER) return '';
    return path.slice(CHAT_HISTORY_FOLDER.length + 1);
  }

  function isNotesPath(path: string): boolean {
    return path === NOTES_FOLDER || path.startsWith(NOTES_FOLDER + '/');
  }

  function getNotesFilter(path: string): string {
    if (path === NOTES_FOLDER) return '';
    return path.slice(NOTES_FOLDER.length + 1);
  }

  function isSubagentsPath(path: string): boolean {
    return path === SUBAGENTS_FOLDER || path.startsWith(SUBAGENTS_FOLDER + '/');
  }

  function getSubagentsFilter(path: string): string {
    if (path === SUBAGENTS_FOLDER) return '';
    return path.slice(SUBAGENTS_FOLDER.length + 1);
  }

  async function fetchFiles(path: string) {
    if (!projectId) return;

    const thisFetchId = ++fetchCounter;
    loading = true;
    lastFetchedPath = path;
    currentPath = path;

    if (isChatHistoryPath(path)) {
      inChatHistoryFolder = true;
      inNotesFolder = false;
      inSubagentsFolder = false;
      await fetchChatHistory(getChatHistoryFilter(path), thisFetchId);
      return;
    }

    if (isNotesPath(path)) {
      inNotesFolder = true;
      inChatHistoryFolder = false;
      inSubagentsFolder = false;
      await fetchNotes(getNotesFilter(path), thisFetchId);
      return;
    }

    if (isSubagentsPath(path)) {
      inSubagentsFolder = true;
      inChatHistoryFolder = false;
      inNotesFolder = false;
      await fetchSubagents(getSubagentsFilter(path), thisFetchId);
      return;
    }

    inChatHistoryFolder = false;
    inNotesFolder = false;
    inSubagentsFolder = false;

    try {
      let fetchedFiles: FileItem[];
      if (worktreeId) {
        const response = await getWorktreeFiles(projectId, worktreeId, path);
        fetchedFiles = response.files.map(f => ({
          name: f.name,
          type: f.type as 'file' | 'directory',
          path: f.path,
          size: f.size
        }));
      } else {
        const params = new URLSearchParams();
        if (path) {
          params.set('path', path);
        }
        if (sessionId) {
          params.set('session_id', sessionId);
        }

        const response = await fetch(`${apiBase()}/projects/${projectId}/files?${params}`, withAuth({ }));

        if (response.ok) {
          const data = await response.json();
          fetchedFiles = data.files;
        } else {
          fetchedFiles = [];
        }
      }

      if (thisFetchId !== fetchCounter) return;

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
          {
            name: SUBAGENTS_FOLDER,
            type: 'directory' as const,
            path: SUBAGENTS_FOLDER,
            size: null
          },
          ...fetchedFiles
        ];
      } else {
        files = fetchedFiles;
      }
    } catch (error) {
      console.error('Failed to fetch files:', error);
      if (thisFetchId === fetchCounter) files = [];
    } finally {
      if (thisFetchId === fetchCounter) loading = false;
    }
  }

  async function fetchChatHistory(search: string = '', fetchId: number) {
    if (!projectId) return;

    try {
      const params = new URLSearchParams();
      if (search) {
        params.set('search', search);
      }
      params.set('limit', '30');
      if (worktreeId) {
        params.set('worktree_id', worktreeId);
      }
      if (sessionId) {
        params.set('session_id', sessionId);
      }

      const response = await fetch(`${apiBase()}/projects/${projectId}/chat-history?${params}`, withAuth({ }));

      if (fetchId !== fetchCounter) return;

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
      if (fetchId === fetchCounter) files = [];
    } finally {
      if (fetchId === fetchCounter) loading = false;
    }
  }

  async function fetchNotes(search: string = '', fetchId: number) {
    try {
      const notes = await getNotesAutocomplete(search || undefined, 30);

      if (fetchId !== fetchCounter) return;

      files = notes.map((note) => ({
        name: note.title,
        type: 'note' as const,
        path: note.absolute_path || `${NOTES_FOLDER}/${note.id}`,
        note_id: note.id,
        preview: note.preview,
        note_tags: note.tags
      }));
    } catch (error) {
      console.error('Failed to fetch notes:', error);
      if (fetchId === fetchCounter) files = [];
    } finally {
      if (fetchId === fetchCounter) loading = false;
    }
  }

  async function fetchSubagents(search: string = '', fetchId: number) {
    if (!profileId) {
      if (fetchId === fetchCounter) {
        files = [];
        loading = false;
      }
      return;
    }

    try {
      const subagents = await getSubagentsForAutocomplete(profileId, projectId, worktreeId, search || undefined, 30);

      if (fetchId !== fetchCounter) return;

      files = subagents.map((agent) => ({
        name: agent.name,
        type: 'subagent' as const,
        path: `${SUBAGENTS_FOLDER}/${agent.id}`,
        subagent_id: agent.id,
        subagent_description: agent.description,
        subagent_source: agent.source,
        subagent_prompt: agent.prompt,
        subagent_tools: agent.tools ?? undefined,
        subagent_model: agent.model
      }));
    } catch (error) {
      console.error('Failed to fetch subagents:', error);
      if (fetchId === fetchCounter) files = [];
    } finally {
      if (fetchId === fetchCounter) loading = false;
    }
  }

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
            navigateToDirectory(selected);
          } else {
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
        const backspaceAtInfo = extractAtMentionQuery(inputValue);
        if (backspaceAtInfo && backspaceAtInfo.query.endsWith('/')) {
          event.preventDefault();
          const pathWithoutTrailingSlash = backspaceAtInfo.query.slice(0, -1);
          const lastSlashIdx = pathWithoutTrailingSlash.lastIndexOf('/');
          if (lastSlashIdx === -1) {
            onSelect({ name: '', type: 'directory', path: '' });
          } else {
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
    onSelect({ ...dir, path: dir.path + '/' });
  }

  function scrollToSelected() {
    if (listElement) {
      const selected = listElement.querySelector(`[data-index="${selectedIndex}"]`) as HTMLElement;
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
    inSubagentsFolder = false;
    fetchFiles('');
  }

  function navigateToPath(path: string) {
    if (!path) {
      goToRoot();
      return;
    }
    onSelect({ name: '', type: 'directory', path: path + '/' });
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
      if (file.name === SUBAGENTS_FOLDER) return 'autocomplete-icon-subagents';
      return 'autocomplete-icon-folder';
    }
    if (file.type === 'chat_history') return 'autocomplete-icon-history';
    if (file.type === 'note') return 'autocomplete-icon-notes';
    if (file.type === 'subagent') return 'autocomplete-icon-subagent';
    return 'autocomplete-icon-file';
  }

  function getBadgeClass(file: FileItem): string {
    if (file.type === 'directory') {
      if (file.name === CHAT_HISTORY_FOLDER) return 'type-history';
      if (file.name === NOTES_FOLDER) return 'type-notes';
      if (file.name === SUBAGENTS_FOLDER) return 'type-subagents';
      return 'type-folder';
    }
    if (file.type === 'chat_history') return 'type-chat';
    if (file.type === 'note') return 'type-note';
    if (file.type === 'subagent') return 'type-subagent';
    return '';
  }

  function getBadgeText(file: FileItem): string {
    if (file.type === 'directory') {
      if (file.name === CHAT_HISTORY_FOLDER) return 'history';
      if (file.name === NOTES_FOLDER) return 'notes';
      if (file.name === SUBAGENTS_FOLDER) return 'agents';
      return 'folder';
    }
    if (file.type === 'chat_history') return 'chat';
    if (file.type === 'note') return 'note';
    if (file.type === 'subagent') return 'agent';
    return '';
  }

  /**
   * Compute the flat index for a file item, accounting for section headers.
   * Quick access items come first, then file items.
   */
  function getFlatIndex(file: FileItem): number {
    if (isAtRoot && showSectionHeaders) {
      const qaIdx = quickAccessItems.indexOf(file);
      if (qaIdx !== -1) return qaIdx;
      const fIdx = fileItems.indexOf(file);
      if (fIdx !== -1) return quickAccessItems.length + fIdx;
    }
    return filteredFiles.indexOf(file);
  }
</script>

{#if visible}
  <div class="autocomplete-panel file-autocomplete-panel">
    <!-- Header -->
    <div class="autocomplete-header">
      <span class="autocomplete-count">
        {#if loading}
          {inChatHistoryFolder ? 'Loading history...' : inNotesFolder ? 'Loading notes...' : inSubagentsFolder ? 'Loading agents...' : 'Loading...'}
        {:else if inChatHistoryFolder}
          {filteredFiles.length} chat{filteredFiles.length !== 1 ? 's' : ''}
        {:else if inNotesFolder}
          {filteredFiles.length} note{filteredFiles.length !== 1 ? 's' : ''}
        {:else if inSubagentsFolder}
          {filteredFiles.length} agent{filteredFiles.length !== 1 ? 's' : ''}
        {:else}
          {filteredFiles.length} item{filteredFiles.length !== 1 ? 's' : ''}
        {/if}
      </span>
      {#if currentPath}
        <nav class="autocomplete-breadcrumb">
          <button type="button" class="autocomplete-breadcrumb-segment" onclick={goToRoot}>@</button>
          {#each breadcrumbSegments as segment, i}
            <span class="autocomplete-breadcrumb-separator">/</span>
            {#if i < breadcrumbSegments.length - 1}
              <button type="button" class="autocomplete-breadcrumb-segment" onclick={() => navigateToPath(segment.path)}>
                {segment.name}
              </button>
            {:else}
              <span class="autocomplete-breadcrumb-current">{segment.name}</span>
            {/if}
          {/each}
        </nav>
      {/if}
    </div>

    <!-- Loading state -->
    {#if loading}
      <div class="autocomplete-loading">
        <div class="autocomplete-loading-spinner"></div>
        <span>Loading...</span>
      </div>
    {:else if filteredFiles.length === 0}
      <!-- Empty state -->
      <div class="autocomplete-empty">
        <svg class="autocomplete-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <span>No matching items</span>
      </div>
    {:else}
      <!-- File list -->
      <ul bind:this={listElement} class="autocomplete-list file-autocomplete-list" role="listbox" aria-label="Files">
        {#if showSectionHeaders}
          <!-- Quick Access section -->
          <li class="autocomplete-section-header" role="presentation">Quick Access</li>
          {#each quickAccessItems as file}
            {@const index = getFlatIndex(file)}
            <li data-index={index} role="option" aria-selected={index === selectedIndex}>
              <button
                type="button"
                class="autocomplete-item"
                class:selected={index === selectedIndex}
                onclick={() => handleSelect(file)}
                onmouseenter={() => selectedIndex = index}
              >
                <div class="autocomplete-item-icon {getIconClass(file)}">
                  {#if file.name === CHAT_HISTORY_FOLDER}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                  {:else if file.name === NOTES_FOLDER}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  {:else if file.name === SUBAGENTS_FOLDER}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                    </svg>
                  {:else}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                    </svg>
                  {/if}
                </div>
                <div class="autocomplete-item-content">
                  <span class="autocomplete-item-name">
                    {#each highlightMatch(file.name, currentFilterQuery) as segment}
                      {#if segment.matched}<span class="autocomplete-match">{segment.text}</span>{:else}{segment.text}{/if}
                    {/each}/
                  </span>
                </div>
                {#if getBadgeText(file)}
                  <span class="autocomplete-badge {getBadgeClass(file)}">{getBadgeText(file)}</span>
                {/if}
              </button>
            </li>
          {/each}

          <!-- Files section -->
          {#if fileItems.length > 0}
            <li class="autocomplete-section-header" role="presentation">Files</li>
            {#each fileItems as file}
              {@const index = getFlatIndex(file)}
              <li data-index={index} role="option" aria-selected={index === selectedIndex}>
                <button
                  type="button"
                  class="autocomplete-item"
                  class:selected={index === selectedIndex}
                  onclick={() => handleSelect(file)}
                  onmouseenter={() => selectedIndex = index}
                >
                  <div class="autocomplete-item-icon {getIconClass(file)}">
                    {#if file.type === 'directory'}
                      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                      </svg>
                    {:else}
                      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                      </svg>
                    {/if}
                  </div>
                  <div class="autocomplete-item-content">
                    <span class="autocomplete-item-name">
                      {#each highlightMatch(file.name, currentFilterQuery) as segment}
                        {#if segment.matched}<span class="autocomplete-match">{segment.text}</span>{:else}{segment.text}{/if}
                      {/each}{file.type === 'directory' ? '/' : ''}
                    </span>
                  </div>
                  {#if file.type === 'file' && file.size}
                    <span class="autocomplete-item-meta">{formatSize(file.size)}</span>
                  {/if}
                  {#if getBadgeText(file)}
                    <span class="autocomplete-badge {getBadgeClass(file)}">{getBadgeText(file)}</span>
                  {/if}
                </button>
              </li>
            {/each}
          {/if}
        {:else}
          <!-- Flat list (inside folder or no section split needed) -->
          {#each filteredFiles as file, index}
            <li data-index={index} role="option" aria-selected={index === selectedIndex}>
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
                    {:else if file.name === SUBAGENTS_FOLDER}
                      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
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
                  {:else if file.type === 'subagent'}
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
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
                    {#each highlightMatch(file.name, currentFilterQuery) as segment}
                      {#if segment.matched}<span class="autocomplete-match">{segment.text}</span>{:else}{segment.text}{/if}
                    {/each}{file.type === 'directory' ? '/' : ''}
                  </span>
                  {#if (file.type === 'chat_history' || file.type === 'note') && file.preview}
                    <span class="autocomplete-item-description">
                      {file.preview}
                    </span>
                  {:else if file.type === 'subagent' && file.subagent_description}
                    <span class="autocomplete-item-description">
                      {file.subagent_description}
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
                    <span>{file.message_count} msgs</span>
                    {#if file.modified_at}
                      <span>·</span>
                      <span>{formatRelativeDate(file.modified_at)}</span>
                    {/if}
                  </span>
                {:else if file.type === 'note' && file.note_tags && file.note_tags.length > 0}
                  <span class="autocomplete-item-meta file-note-tags">
                    {file.note_tags.slice(0, 2).join(', ')}{file.note_tags.length > 2 ? '...' : ''}
                  </span>
                {:else if file.type === 'subagent' && file.subagent_source}
                  <span class="autocomplete-item-meta">
                    {file.subagent_source}
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
        {/if}
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
  .file-autocomplete-panel {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: 8px;
    max-height: 20rem;
    z-index: 50;
    background: var(--popover, var(--card));
  }

  .file-autocomplete-list {
    max-height: 14rem;
  }

  .file-note-tags {
    max-width: 6rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
