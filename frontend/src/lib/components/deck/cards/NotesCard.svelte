<script lang="ts">
	/**
	 * NotesCard - Global notes for ideas, bugs, planning
	 *
	 * Provides list view with search/filter, and editor view for editing notes.
	 */

	import BaseCard from './BaseCard.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import type { DeckCard } from './types';
	import './card-design-system.css';
	import { StickyNote, Search as SearchIcon, Plus } from 'lucide-svelte';
	import {
		getNotes,
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
		embedded?: boolean;
	}

	let { card, onClose, onMaximize, onFocus, onMove, onResize, onDragEnd, onResizeEnd, mobile = false, embedded = false }: Props = $props();

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
	let textareaEl = $state<HTMLTextAreaElement | null>(null);

	// Debounce for auto-save
	let saveTimeout: ReturnType<typeof setTimeout> | null = null;
	let hasUnsavedChanges = $state(false);

	// Load notes and tags
	async function loadData() {
		loading = true;
		error = null;

		try {
			const [notesData, tagsData] = await Promise.all([
				getNotes({
					search: searchQuery || undefined,
					tag: selectedTag || undefined
				}),
				getNoteTags()
			]);
			notes = notesData;
			tags = tagsData;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load notes';
		} finally {
			loading = false;
		}
	}

	// Load data on mount
	onMount(() => {
		loadData();
		return () => {
			if (saveTimeout) {
				clearTimeout(saveTimeout);
			}
		};
	});

	// Reload when search/filter changes
	$effect(() => {
		// Access these to establish reactivity
		searchQuery;
		selectedTag;

		if (!editingNote && !isCreatingNew) {
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
		if (!editorTitle.trim()) return;

		saving = true;
		try {
			if (isCreatingNew) {
				// Create new note and switch to editing mode
				const newNote = await createNote({
					title: editorTitle.trim(),
					content: editorContent,
					tags: editorTags,
					is_pinned: editorPinned
				});
				// Switch from creating to editing the newly created note
				isCreatingNew = false;
				editingNote = newNote;
			} else if (editingNote) {
				// Update existing note and stay in editor
				const updatedNote = await updateNote(editingNote.id, {
					title: editorTitle.trim(),
					content: editorContent,
					tags: editorTags,
					is_pinned: editorPinned
				});
				// Update the editing note reference with latest data
				editingNote = updatedNote;
			}
			hasUnsavedChanges = false;
			// Don't close editor - user stays in edit mode
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save note';
		} finally {
			saving = false;
		}
	}

	// Delete the current note
	async function handleDelete() {
		if (!editingNote) return;
		if (!confirm('Delete this note? This cannot be undone.')) return;

		try {
			await deleteNote(editingNote.id);
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

		try {
			await updateNote(note.id, { is_pinned: !note.is_pinned });
			loadData();
		} catch (e) {
			// Silent fail for pin toggle
		}
	}

	// Auto-resize textarea to fit content
	function autoResizeTextarea() {
		if (!textareaEl) return;
		textareaEl.style.height = 'auto';
		textareaEl.style.height = textareaEl.scrollHeight + 'px';
	}

	// Auto-resize when textarea is bound or content changes externally (e.g. opening a note)
	$effect(() => {
		if (textareaEl && editorContent !== undefined) {
			// Tick delay so the DOM has updated
			requestAnimationFrame(autoResizeTextarea);
		}
	});

	// Mark as having unsaved changes and trigger autosave
	function markDirty() {
		hasUnsavedChanges = true;

		// Clear existing timeout
		if (saveTimeout) {
			clearTimeout(saveTimeout);
		}

		// Only autosave if we have a title (required field)
		if (editorTitle.trim()) {
			saveTimeout = setTimeout(() => {
				saveNote();
			}, 1000); // Autosave after 1 second of inactivity
		}
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
						<div class="note-item-icon">
							<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
								<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
								<polyline points="14 2 14 8 20 8"/>
								<line x1="16" y1="13" x2="8" y2="13"/>
								<line x1="16" y1="17" x2="8" y2="17"/>
								<polyline points="10 9 9 9 8 9"/>
							</svg>
						</div>
						<div class="note-item-content">
							<div class="note-header">
								{#if note.is_pinned}
									<span class="pin-icon">
										<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
											<line x1="12" y1="17" x2="12" y2="22"/>
											<path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>
										</svg>
									</span>
								{/if}
								<span class="note-title">{note.title}</span>
								<button class="pin-btn" onclick={(e) => togglePin(note, e)} title={note.is_pinned ? 'Unpin' : 'Pin'}>
									<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
										<line x1="12" y1="17" x2="12" y2="22"/>
										<path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>
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
							<span class="note-date">{formatRelativeTime(note.updated_at)}</span>
							</div>
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
				{#if saving}
					<span class="autosave-status">Saving...</span>
				{/if}
				{#if editingNote}
					<button class="delete-btn" onclick={handleDelete} title="Delete note">
						<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<polyline points="3 6 5 6 21 6"/>
							<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
						</svg>
					</button>
				{/if}
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
					bind:this={textareaEl}
					oninput={() => { markDirty(); autoResizeTextarea(); }}
				></textarea>
			{/if}
		</div>

	</div>
{/snippet}

{#if embedded}
	<FeaturePageLayout
		icon={StickyNote}
		title="Notes"
		itemCount={notes.length}
		itemLabel={notes.length === 1 ? 'note' : 'notes'}
		breadcrumbs={editingNote || isCreatingNew ? [
			{ label: 'Notes', onclick: closeEditor },
			{ label: editingNote ? editorTitle || 'Untitled' : 'New Note' }
		] : undefined}
	>
		{#snippet headerActions()}
			{#if !(editingNote || isCreatingNew)}
				<div class="fph-search-wrapper">
					<SearchIcon size={14} />
					<input
						type="text"
						placeholder="Search notes..."
						bind:value={searchQuery}
						class="fph-search-input"
					/>
				</div>
				<button class="fp-btn fp-btn--primary" onclick={startNewNote} title="New Note">
					<Plus size={14} />
					<span class="hidden sm:inline">New Note</span>
				</button>
			{/if}
		{/snippet}
		{#snippet children()}
			<div class="notes-card-content card-system">
				{#if editingNote || isCreatingNew}
					{@render editorView()}
				{:else}
					{@render listView()}
				{/if}
			</div>
		{/snippet}
	</FeaturePageLayout>
{:else if mobile}
	<div class="notes-card-content card-system mobile">
		{#if editingNote || isCreatingNew}
			{@render editorView()}
		{:else}
			{@render listView()}
		{/if}
	</div>
{:else}
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="notes-card-content card-system">
			{#if editingNote || isCreatingNew}
				{@render editorView()}
			{:else}
				{@render listView()}
			{/if}
		</div>
	</BaseCard>
{/if}

<style>
	/* ============================================
	   NOTES CARD - Uses card-design-system.css tokens
	   ============================================ */
	.notes-card-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-height: 0;
		background: var(--surface-0);
		border-radius: 0 0 var(--radius-lg) var(--radius-lg);
	}

	.notes-card-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* ============================================
	   LIST VIEW
	   ============================================ */
	.notes-list {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.notes-toolbar {
		display: flex;
		gap: var(--space-2);
		padding: var(--space-3);
		border-bottom: 1px solid var(--border-default);
	}

	.search-box {
		flex: 1;
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		background: var(--surface-1);
		border-radius: var(--radius-md);
		color: var(--text-secondary);
	}

	.search-input {
		flex: 1;
		background: none;
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md);
		outline: none;
	}

	.search-input::placeholder {
		color: var(--text-secondary);
	}

	.new-note-btn {
		display: flex;
		align-items: center;
		gap: calc(var(--space-1) * 1.5);
		padding: var(--space-2) var(--space-3);
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: var(--radius-md);
		font-size: var(--text-md);
		font-weight: 500;
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.new-note-btn:hover:not(:disabled) {
		opacity: 0.9;
		box-shadow: var(--shadow-glow-soft);
	}

	.new-note-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* ============================================
	   TAG FILTER
	   ============================================ */
	.tag-filter {
		display: flex;
		flex-wrap: wrap;
		gap: calc(var(--space-1) * 1.5);
		padding: var(--space-2) var(--space-3);
		border-bottom: 1px solid var(--border-default);
	}

	.tag-pill {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-1) calc(var(--space-2) + 2px);
		background: var(--surface-1);
		border: 1px solid transparent;
		border-radius: var(--radius-full);
		color: var(--text-secondary);
		font-size: var(--text-sm);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.tag-pill:hover {
		background: var(--surface-hover);
		border-color: var(--border-subtle);
	}

	.tag-pill.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		box-shadow: var(--selection-shadow);
	}

	.tag-count {
		font-size: var(--text-xs);
		opacity: 0.7;
	}

	/* ============================================
	   NOTES SCROLL & ITEMS
	   ============================================ */
	.notes-scroll {
		flex: 1;
		overflow-y: auto;
		padding: var(--space-2);
	}

	.note-item {
		display: flex;
		align-items: flex-start;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		margin-bottom: var(--space-2);
		text-align: left;
		cursor: pointer;
		transition: all var(--transition-smooth);
		box-shadow: var(--shadow-inset), var(--shadow-xs);
		position: relative;
		overflow: hidden;
	}

	.note-item::before {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--primary) 4%, transparent) 0%,
			transparent 50%
		);
		opacity: 0;
		transition: opacity var(--transition-smooth);
		pointer-events: none;
	}

	.note-item:hover {
		border-color: color-mix(in oklch, var(--accent) 50%, var(--border));
		box-shadow: var(--shadow-inset), 0 0 12px color-mix(in oklch, var(--accent) 20%, transparent);
		transform: translateY(-1px);
	}

	.note-item:hover::before {
		opacity: 1;
	}

	.note-item:active {
		transform: translateY(0);
		box-shadow: var(--shadow-inset), var(--shadow-sm);
	}

	/* Icon box for note items */
	.note-item-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		flex-shrink: 0;
		border-radius: var(--radius-md);
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--primary) 18%, transparent) 0%,
			color-mix(in oklch, var(--primary) 8%, transparent) 100%
		);
		border: 1px solid color-mix(in oklch, var(--primary) 25%, transparent);
		box-shadow: var(--shadow-xs), var(--shadow-glow-soft);
		color: var(--primary);
		transition: all var(--transition-smooth);
	}

	.note-item:hover .note-item-icon {
		box-shadow: var(--shadow-sm), var(--shadow-glow-primary);
		transform: scale(1.02);
	}

	/* Content wrapper */
	.note-item-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: calc(var(--space-1) * 1.5);
	}

	.note-header {
		display: flex;
		align-items: center;
		gap: calc(var(--space-1) * 1.5);
	}

	.pin-icon {
		color: var(--text-accent);
		flex-shrink: 0;
	}

	.note-title {
		flex: 1;
		font-weight: 500;
		font-size: var(--text-md);
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pin-btn {
		padding: var(--space-2);
		background: none;
		border: none;
		color: var(--text-secondary);
		cursor: pointer;
		opacity: 0;
		transition: all var(--transition-smooth);
		min-width: 32px;
		min-height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		border-radius: var(--radius-sm);
	}

	.note-item:hover .pin-btn {
		opacity: 1;
	}

	.pin-btn:hover {
		color: var(--text-accent);
		background: var(--surface-active);
	}

	/* Touch devices / mobile: always show pin button with larger target */
	@media (pointer: coarse), (max-width: 639px) {
		.pin-btn {
			opacity: 1;
			min-width: 44px;
			min-height: 44px;
		}
	}

	.note-meta {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		color: var(--text-secondary);
	}

	.note-tags {
		display: flex;
		gap: var(--space-1);
	}

	.note-tag {
		padding: 2px calc(var(--space-1) * 1.5);
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border-radius: var(--radius-sm);
		font-size: var(--text-xs);
	}

	.note-tag.more {
		background: var(--surface-1);
		color: var(--text-secondary);
	}

	.linked-sessions {
		display: flex;
		align-items: center;
		gap: var(--space-1);
	}

	.note-date {
		margin-left: auto;
	}

	/* ============================================
	   EMPTY & LOADING STATES
	   ============================================ */
	.empty-state,
	.loading-state,
	.error-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-3);
		height: 200px;
		color: var(--text-secondary);
		text-align: center;
	}

	.error-state {
		color: var(--destructive);
	}

	.create-first-btn {
		padding: var(--space-2) var(--space-4);
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: var(--radius-md);
		font-size: var(--text-md);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.create-first-btn:hover {
		box-shadow: var(--shadow-glow-soft);
	}

	/* ============================================
	   FOOTER
	   ============================================ */
	.notes-footer {
		padding: var(--space-2) var(--space-3);
		border-top: 1px solid var(--border-default);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		text-align: center;
	}

	/* ============================================
	   EDITOR VIEW
	   ============================================ */
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
		padding: var(--space-3);
		border-bottom: 1px solid var(--border-default);
	}

	.back-btn {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		padding: calc(var(--space-1) * 1.5) var(--space-3);
		background: var(--surface-1);
		border: none;
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		font-size: var(--text-md);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.back-btn:hover {
		background: var(--surface-hover);
	}

	.editor-actions {
		display: flex;
		gap: var(--space-2);
	}

	.delete-btn {
		padding: calc(var(--space-1) * 1.5) calc(var(--space-2) + 2px);
		background: var(--surface-1);
		border: none;
		border-radius: var(--radius-sm);
		color: var(--destructive);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.delete-btn:hover {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}

	.autosave-status {
		font-size: var(--text-sm);
		color: var(--text-secondary);
		display: flex;
		align-items: center;
		gap: var(--space-1);
	}

	.title-input {
		padding: var(--space-3);
		background: none;
		border: none;
		border-bottom: 1px solid var(--border-default);
		color: var(--text-primary);
		font-size: var(--text-xl);
		font-weight: 600;
		outline: none;
	}

	.title-input::placeholder {
		color: var(--text-secondary);
	}

	.title-input:focus {
		border-bottom-color: var(--border-emphasis);
	}

	/* ============================================
	   TAGS SECTION
	   ============================================ */
	.tags-section {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-2) var(--space-3);
		border-bottom: 1px solid var(--border-default);
	}

	.tags-list {
		flex: 1;
		display: flex;
		flex-wrap: wrap;
		gap: calc(var(--space-1) * 1.5);
		align-items: center;
	}

	.editor-tag {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-1) var(--space-2);
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border-radius: var(--radius-sm);
		font-size: var(--text-sm);
	}

	.remove-tag {
		background: none;
		border: none;
		color: inherit;
		font-size: var(--text-md);
		cursor: pointer;
		padding: 0;
		line-height: 1;
		opacity: 0.7;
		transition: opacity var(--transition-fast);
	}

	.remove-tag:hover {
		opacity: 1;
	}

	.tag-input {
		min-width: 80px;
		padding: var(--space-1) var(--space-2);
		background: none;
		border: 1px dashed var(--border-default);
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		font-size: var(--text-sm);
		outline: none;
		transition: all var(--transition-smooth);
	}

	.tag-input:focus {
		border-style: solid;
		border-color: var(--border-emphasis);
	}

	.pin-toggle {
		display: flex;
		align-items: center;
		gap: calc(var(--space-1) * 1.5);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		cursor: pointer;
		white-space: nowrap;
	}

	.pin-toggle input {
		accent-color: var(--primary);
	}

	/* ============================================
	   EDITOR TABS
	   ============================================ */
	.editor-tabs {
		display: flex;
		border-bottom: 1px solid var(--border-default);
	}

	.editor-tabs button {
		flex: 1;
		padding: var(--space-2);
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--text-secondary);
		font-size: var(--text-base);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.editor-tabs button:hover {
		color: var(--text-primary);
		background: var(--surface-hover);
	}

	.editor-tabs button.active {
		color: var(--text-accent);
		border-bottom-color: var(--primary);
	}

	/* ============================================
	   CONTENT AREA
	   ============================================ */
	.content-area {
		flex: 1;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}

	.content-textarea {
		width: 100%;
		min-height: 120px;
		padding: var(--space-3);
		background: none;
		border: none;
		color: var(--text-primary);
		font-family: var(--font-mono);
		font-size: var(--text-md);
		line-height: 1.6;
		resize: none;
		outline: none;
		overflow: hidden;
	}

	.content-textarea::placeholder {
		color: var(--text-secondary);
	}

	.preview-content {
		flex: 1;
		padding: var(--space-3);
		overflow-y: auto;
		line-height: 1.6;
		color: var(--text-primary);
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
		padding: var(--space-3);
		background: var(--surface-1);
		border-radius: var(--radius-sm);
		overflow-x: auto;
	}

	.preview-content :global(code) {
		padding: 2px calc(var(--space-1) * 1.5);
		background: var(--surface-1);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: 0.9em;
	}

	.preview-content :global(pre code) {
		padding: 0;
		background: none;
	}

	.preview-content :global(li) {
		margin-left: var(--space-5);
	}

	.preview-empty {
		color: var(--text-secondary);
		font-style: italic;
	}


</style>
