<script lang="ts">
	/**
	 * NotesCard - Project-scoped notes for ideas, bugs, planning
	 *
	 * Provides list view with search/filter, and editor view for editing notes.
	 */

	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { selectedProject } from '$lib/stores/chat';
	import {
		getProjectNotes,
		createNote,
		updateNote,
		deleteNote,
		getNoteTags,
		type Note,
		type NoteTag
	} from '$lib/api/client';
	import { onMount } from 'svelte';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let { card, onClose, onMaximize, onFocus, onMove, onResize, onDragEnd, onResizeEnd, mobile = false }: Props = $props();

	// State
	let notes = $state<Note[]>([]);
	let tags = $state<NoteTag[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Filter state
	let searchQuery = $state('');
	let selectedTag = $state<string | null>(null);

	// Editor state
	let editingNote = $state<Note | null>(null);
	let isCreatingNew = $state(false);
	let editorTitle = $state('');
	let editorContent = $state('');
	let editorTags = $state<string[]>([]);
	let editorPinned = $state(false);
	let saving = $state(false);
	let showPreview = $state(false);
	let newTagInput = $state('');

	// Debounce for auto-save
	let saveTimeout: ReturnType<typeof setTimeout> | null = null;
	let hasUnsavedChanges = $state(false);

	// Derived - selectedProject is already the project ID string
	const projectId = $derived($selectedProject || null);

	// Load notes and tags
	async function loadData() {
		if (!projectId) {
			notes = [];
			tags = [];
			loading = false;
			return;
		}

		loading = true;
		error = null;

		try {
			const [notesData, tagsData] = await Promise.all([
				getProjectNotes(projectId, {
					search: searchQuery || undefined,
					tag: selectedTag || undefined
				}),
				getNoteTags(projectId)
			]);
			notes = notesData;
			tags = tagsData;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load notes';
		} finally {
			loading = false;
		}
	}

	// Watch for project change
	$effect(() => {
		if (projectId) {
			loadData();
		}
	});

	// Reload when search/filter changes
	$effect(() => {
		// Access these to establish reactivity
		searchQuery;
		selectedTag;

		if (projectId && !editingNote && !isCreatingNew) {
			// Debounce search
			const timeout = setTimeout(loadData, 300);
			return () => clearTimeout(timeout);
		}
	});

	// Start creating a new note
	function startNewNote() {
		isCreatingNew = true;
		editingNote = null;
		editorTitle = '';
		editorContent = '';
		editorTags = [];
		editorPinned = false;
		hasUnsavedChanges = false;
	}

	// Open a note for editing
	function openNote(note: Note) {
		editingNote = note;
		isCreatingNew = false;
		editorTitle = note.title;
		editorContent = note.content;
		editorTags = [...note.tags];
		editorPinned = note.is_pinned;
		hasUnsavedChanges = false;
	}

	// Close editor and go back to list
	function closeEditor() {
		if (hasUnsavedChanges && !confirm('You have unsaved changes. Discard them?')) {
			return;
		}
		editingNote = null;
		isCreatingNew = false;
		loadData();
	}

	// Save the current note (create or update)
	async function saveNote() {
		if (!projectId || !editorTitle.trim()) return;

		saving = true;
		try {
			if (isCreatingNew) {
				await createNote(projectId, {
					title: editorTitle.trim(),
					content: editorContent,
					tags: editorTags,
					is_pinned: editorPinned
				});
			} else if (editingNote) {
				await updateNote(projectId, editingNote.id, {
					title: editorTitle.trim(),
					content: editorContent,
					tags: editorTags,
					is_pinned: editorPinned
				});
			}
			hasUnsavedChanges = false;
			closeEditor();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save note';
		} finally {
			saving = false;
		}
	}

	// Delete the current note
	async function handleDelete() {
		if (!projectId || !editingNote) return;
		if (!confirm('Delete this note? This cannot be undone.')) return;

		try {
			await deleteNote(projectId, editingNote.id);
			editingNote = null;
			isCreatingNew = false;
			loadData();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete note';
		}
	}

	// Toggle pin status
	async function togglePin(note: Note, e: Event) {
		e.stopPropagation();
		if (!projectId) return;

		try {
			await updateNote(projectId, note.id, { is_pinned: !note.is_pinned });
			loadData();
		} catch (e) {
			// Silent fail for pin toggle
		}
	}

	// Mark as having unsaved changes
	function markDirty() {
		hasUnsavedChanges = true;
	}

	// Add a tag
	function addTag() {
		const tag = newTagInput.trim().toLowerCase().replace(/\s+/g, '-');
		if (tag && !editorTags.includes(tag)) {
			editorTags = [...editorTags, tag];
			markDirty();
		}
		newTagInput = '';
	}

	// Remove a tag
	function removeTag(tag: string) {
		editorTags = editorTags.filter(t => t !== tag);
		markDirty();
	}

	// Format relative time
	function formatRelativeTime(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const days = Math.floor(diff / (1000 * 60 * 60 * 24));

		if (days === 0) return 'Today';
		if (days === 1) return 'Yesterday';
		if (days < 7) return `${days} days ago`;
		if (days < 30) return `${Math.floor(days / 7)} weeks ago`;
		return date.toLocaleDateString();
	}

	// Simple markdown to HTML (basic conversion)
	function renderMarkdown(content: string): string {
		return content
			// Headers
			.replace(/^### (.+)$/gm, '<h3>$1</h3>')
			.replace(/^## (.+)$/gm, '<h2>$1</h2>')
			.replace(/^# (.+)$/gm, '<h1>$1</h1>')
			// Bold and italic
			.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
			.replace(/\*(.+?)\*/g, '<em>$1</em>')
			// Code blocks
			.replace(/```[\s\S]*?```/g, (match) => {
				const content = match.slice(3, -3).trim();
				return `<pre><code>${content.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</code></pre>`;
			})
			// Inline code
			.replace(/`(.+?)`/g, '<code>$1</code>')
			// Lists
			.replace(/^- (.+)$/gm, '<li>$1</li>')
			// Paragraphs
			.replace(/\n\n/g, '</p><p>')
			.replace(/^(.+)$/gm, (match) => {
				if (match.startsWith('<')) return match;
				return match;
			});
	}
</script>

{#snippet listView()}
	<div class="notes-list">
		<!-- Search and New Button -->
		<div class="notes-toolbar">
			<div class="search-box">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="11" cy="11" r="8"/>
					<path d="M21 21l-4.35-4.35"/>
				</svg>
				<input
					type="text"
					placeholder="Search notes..."
					bind:value={searchQuery}
					class="search-input"
				/>
			</div>
			<button class="new-note-btn" onclick={startNewNote} disabled={!projectId}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<line x1="12" y1="5" x2="12" y2="19"/>
					<line x1="5" y1="12" x2="19" y2="12"/>
				</svg>
				New Note
			</button>
		</div>

		<!-- Tag Filter -->
		{#if tags.length > 0}
			<div class="tag-filter">
				<button
					class="tag-pill"
					class:active={selectedTag === null}
					onclick={() => selectedTag = null}
				>
					all
				</button>
				{#each tags as tag}
					<button
						class="tag-pill"
						class:active={selectedTag === tag.name}
						onclick={() => selectedTag = selectedTag === tag.name ? null : tag.name}
					>
						{tag.name}
						<span class="tag-count">{tag.count}</span>
					</button>
				{/each}
			</div>
		{/if}

		<!-- Notes List -->
		<div class="notes-scroll">
			{#if loading}
				<div class="loading-state">Loading notes...</div>
			{:else if error}
				<div class="error-state">{error}</div>
			{:else if !projectId}
				<div class="empty-state">
					<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
						<polyline points="14 2 14 8 20 8"/>
						<line x1="16" y1="13" x2="8" y2="13"/>
						<line x1="16" y1="17" x2="8" y2="17"/>
						<polyline points="10 9 9 9 8 9"/>
					</svg>
					<p>Select a project to view notes</p>
				</div>
			{:else if notes.length === 0}
				<div class="empty-state">
					<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
						<polyline points="14 2 14 8 20 8"/>
						<line x1="16" y1="13" x2="8" y2="13"/>
						<line x1="16" y1="17" x2="8" y2="17"/>
						<polyline points="10 9 9 9 8 9"/>
					</svg>
					<p>{searchQuery || selectedTag ? 'No notes match your search' : 'No notes yet'}</p>
					<button class="create-first-btn" onclick={startNewNote}>Create your first note</button>
				</div>
			{:else}
				{#each notes as note}
					<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
					<div class="note-item" onclick={() => openNote(note)} role="button" tabindex="0" onkeydown={(e) => e.key === 'Enter' && openNote(note)}>
						<div class="note-header">
							{#if note.is_pinned}
								<span class="pin-icon">
									<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
										<path d="M16 3h-2v6.586l-4.707-4.707-1.414 1.414L12.586 11l-7.293 7.293 1.414 1.414L14 12.414V19h2V3z"/>
									</svg>
								</span>
							{/if}
							<span class="note-title">{note.title}</span>
							<button class="pin-btn" onclick={(e) => togglePin(note, e)} title={note.is_pinned ? 'Unpin' : 'Pin'}>
								<svg width="14" height="14" viewBox="0 0 24 24" fill={note.is_pinned ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2">
									<path d="M16 3h-2v6.586l-4.707-4.707-1.414 1.414L12.586 11l-7.293 7.293 1.414 1.414L14 12.414V19h2V3z"/>
								</svg>
							</button>
						</div>
						<div class="note-meta">
							{#if note.tags.length > 0}
								<span class="note-tags">
									{#each note.tags.slice(0, 3) as tag}
										<span class="note-tag">{tag}</span>
									{/each}
									{#if note.tags.length > 3}
										<span class="note-tag more">+{note.tags.length - 3}</span>
									{/if}
								</span>
							{/if}
							{#if note.linked_sessions.length > 0}
								<span class="linked-sessions">{note.linked_sessions.length} session{note.linked_sessions.length === 1 ? '' : 's'}</span>
							{/if}
							<span class="note-date">{formatRelativeTime(note.updated_at)}</span>
						</div>
					</div>
				{/each}
			{/if}
		</div>

		<!-- Footer -->
		<div class="notes-footer">
			{notes.length} note{notes.length === 1 ? '' : 's'}
		</div>
	</div>
{/snippet}

{#snippet editorView()}
	<div class="note-editor">
		<!-- Editor Header -->
		<div class="editor-header">
			<button class="back-btn" onclick={closeEditor}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="15 18 9 12 15 6"/>
				</svg>
				Back
			</button>
			<div class="editor-actions">
				{#if editingNote}
					<button class="delete-btn" onclick={handleDelete} title="Delete note">
						<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<polyline points="3 6 5 6 21 6"/>
							<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
						</svg>
					</button>
				{/if}
				<button
					class="save-btn"
					onclick={saveNote}
					disabled={saving || !editorTitle.trim()}
				>
					{saving ? 'Saving...' : 'Save'}
				</button>
			</div>
		</div>

		<!-- Title Input -->
		<input
			type="text"
			class="title-input"
			placeholder="Note title..."
			bind:value={editorTitle}
			oninput={markDirty}
		/>

		<!-- Tags -->
		<div class="tags-section">
			<div class="tags-list">
				{#each editorTags as tag}
					<span class="editor-tag">
						{tag}
						<button class="remove-tag" onclick={() => removeTag(tag)}>×</button>
					</span>
				{/each}
				<input
					type="text"
					class="tag-input"
					placeholder="Add tag..."
					bind:value={newTagInput}
					onkeydown={(e) => {
						if (e.key === 'Enter') {
							e.preventDefault();
							addTag();
						}
					}}
					onblur={addTag}
				/>
			</div>
			<label class="pin-toggle">
				<input type="checkbox" bind:checked={editorPinned} onchange={markDirty} />
				Pin to top
			</label>
		</div>

		<!-- Editor/Preview Toggle -->
		<div class="editor-tabs">
			<button class:active={!showPreview} onclick={() => showPreview = false}>Edit</button>
			<button class:active={showPreview} onclick={() => showPreview = true}>Preview</button>
		</div>

		<!-- Content Area -->
		<div class="content-area">
			{#if showPreview}
				<div class="preview-content">
					{#if editorContent}
						{@html renderMarkdown(editorContent)}
					{:else}
						<p class="preview-empty">Nothing to preview</p>
					{/if}
				</div>
			{:else}
				<textarea
					class="content-textarea"
					placeholder="Write your note in Markdown..."
					bind:value={editorContent}
					oninput={markDirty}
				></textarea>
			{/if}
		</div>

		{#if hasUnsavedChanges}
			<div class="unsaved-indicator">Unsaved changes</div>
		{/if}
	</div>
{/snippet}

{#if mobile}
	<div class="notes-card-content mobile">
		{#if editingNote || isCreatingNew}
			{@render editorView()}
		{:else}
			{@render listView()}
		{/if}
	</div>
{:else}
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="notes-card-content">
			{#if editingNote || isCreatingNew}
				{@render editorView()}
			{:else}
				{@render listView()}
			{/if}
		</div>
	</BaseCard>
{/if}

<style>
	.notes-card-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-height: 0;
		background: var(--card);
		border-radius: 0 0 12px 12px;
	}

	.notes-card-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* List View */
	.notes-list {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.notes-toolbar {
		display: flex;
		gap: 8px;
		padding: 12px;
		border-bottom: 1px solid var(--border);
	}

	.search-box {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: var(--muted);
		border-radius: 8px;
		color: var(--muted-foreground);
	}

	.search-input {
		flex: 1;
		background: none;
		border: none;
		color: var(--foreground);
		font-size: 14px;
		outline: none;
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.new-note-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: 8px;
		font-size: 14px;
		font-weight: 500;
		cursor: pointer;
		transition: opacity 0.2s;
	}

	.new-note-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.new-note-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Tag Filter */
	.tag-filter {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
	}

	.tag-pill {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 10px;
		background: var(--muted);
		border: 1px solid transparent;
		border-radius: 16px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: all 0.2s;
	}

	.tag-pill:hover {
		background: var(--accent);
	}

	.tag-pill.active {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.tag-count {
		font-size: 10px;
		opacity: 0.7;
	}

	/* Notes Scroll */
	.notes-scroll {
		flex: 1;
		overflow-y: auto;
		padding: 8px;
	}

	.note-item {
		display: flex;
		flex-direction: column;
		gap: 6px;
		width: 100%;
		padding: 12px;
		background: var(--muted);
		border: 1px solid transparent;
		border-radius: 10px;
		margin-bottom: 8px;
		text-align: left;
		cursor: pointer;
		transition: all 0.2s;
	}

	.note-item:hover {
		border-color: var(--border);
		background: var(--accent);
	}

	.note-header {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.pin-icon {
		color: var(--primary);
		flex-shrink: 0;
	}

	.note-title {
		flex: 1;
		font-weight: 500;
		font-size: 14px;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pin-btn {
		padding: 4px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.2s;
	}

	.note-item:hover .pin-btn {
		opacity: 1;
	}

	.pin-btn:hover {
		color: var(--primary);
	}

	.note-meta {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 12px;
		color: var(--muted-foreground);
	}

	.note-tags {
		display: flex;
		gap: 4px;
	}

	.note-tag {
		padding: 2px 6px;
		background: rgba(34, 197, 94, 0.15);
		color: rgb(34, 197, 94);
		border-radius: 4px;
		font-size: 11px;
	}

	.note-tag.more {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.linked-sessions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.note-date {
		margin-left: auto;
	}

	/* Empty and Loading States */
	.empty-state,
	.loading-state,
	.error-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		height: 200px;
		color: var(--muted-foreground);
		text-align: center;
	}

	.error-state {
		color: var(--destructive);
	}

	.create-first-btn {
		padding: 8px 16px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: 8px;
		font-size: 14px;
		cursor: pointer;
	}

	/* Footer */
	.notes-footer {
		padding: 8px 12px;
		border-top: 1px solid var(--border);
		font-size: 12px;
		color: var(--muted-foreground);
		text-align: center;
	}

	/* Editor View */
	.note-editor {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.editor-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px;
		border-bottom: 1px solid var(--border);
	}

	.back-btn {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 12px;
		background: var(--muted);
		border: none;
		border-radius: 6px;
		color: var(--foreground);
		font-size: 14px;
		cursor: pointer;
	}

	.back-btn:hover {
		background: var(--accent);
	}

	.editor-actions {
		display: flex;
		gap: 8px;
	}

	.delete-btn {
		padding: 6px 10px;
		background: var(--muted);
		border: none;
		border-radius: 6px;
		color: var(--destructive);
		cursor: pointer;
	}

	.delete-btn:hover {
		background: rgba(239, 68, 68, 0.1);
	}

	.save-btn {
		padding: 6px 16px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: 6px;
		font-size: 14px;
		font-weight: 500;
		cursor: pointer;
	}

	.save-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.title-input {
		padding: 12px;
		background: none;
		border: none;
		border-bottom: 1px solid var(--border);
		color: var(--foreground);
		font-size: 18px;
		font-weight: 600;
		outline: none;
	}

	.title-input::placeholder {
		color: var(--muted-foreground);
	}

	/* Tags Section */
	.tags-section {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
	}

	.tags-list {
		flex: 1;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
	}

	.editor-tag {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		background: rgba(34, 197, 94, 0.15);
		color: rgb(34, 197, 94);
		border-radius: 4px;
		font-size: 12px;
	}

	.remove-tag {
		background: none;
		border: none;
		color: inherit;
		font-size: 14px;
		cursor: pointer;
		padding: 0;
		line-height: 1;
	}

	.tag-input {
		min-width: 80px;
		padding: 4px 8px;
		background: none;
		border: 1px dashed var(--border);
		border-radius: 4px;
		color: var(--foreground);
		font-size: 12px;
		outline: none;
	}

	.tag-input:focus {
		border-style: solid;
		border-color: var(--primary);
	}

	.pin-toggle {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--muted-foreground);
		cursor: pointer;
		white-space: nowrap;
	}

	.pin-toggle input {
		accent-color: var(--primary);
	}

	/* Editor Tabs */
	.editor-tabs {
		display: flex;
		border-bottom: 1px solid var(--border);
	}

	.editor-tabs button {
		flex: 1;
		padding: 8px;
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--muted-foreground);
		font-size: 13px;
		cursor: pointer;
		transition: all 0.2s;
	}

	.editor-tabs button:hover {
		color: var(--foreground);
	}

	.editor-tabs button.active {
		color: var(--primary);
		border-bottom-color: var(--primary);
	}

	/* Content Area */
	.content-area {
		flex: 1;
		overflow: hidden;
		display: flex;
	}

	.content-textarea {
		flex: 1;
		padding: 12px;
		background: none;
		border: none;
		color: var(--foreground);
		font-family: 'Fira Code', 'Monaco', monospace;
		font-size: 14px;
		line-height: 1.6;
		resize: none;
		outline: none;
	}

	.content-textarea::placeholder {
		color: var(--muted-foreground);
	}

	.preview-content {
		flex: 1;
		padding: 12px;
		overflow-y: auto;
		line-height: 1.6;
		color: var(--foreground);
	}

	.preview-content :global(h1),
	.preview-content :global(h2),
	.preview-content :global(h3) {
		margin: 1em 0 0.5em;
		font-weight: 600;
	}

	.preview-content :global(h1) { font-size: 1.5em; }
	.preview-content :global(h2) { font-size: 1.3em; }
	.preview-content :global(h3) { font-size: 1.1em; }

	.preview-content :global(pre) {
		padding: 12px;
		background: var(--muted);
		border-radius: 6px;
		overflow-x: auto;
	}

	.preview-content :global(code) {
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 4px;
		font-family: 'Fira Code', monospace;
		font-size: 0.9em;
	}

	.preview-content :global(pre code) {
		padding: 0;
		background: none;
	}

	.preview-content :global(li) {
		margin-left: 20px;
	}

	.preview-empty {
		color: var(--muted-foreground);
		font-style: italic;
	}

	/* Unsaved Indicator */
	.unsaved-indicator {
		padding: 4px 12px;
		background: rgba(234, 179, 8, 0.1);
		color: rgb(234, 179, 8);
		font-size: 12px;
		text-align: center;
	}
</style>
