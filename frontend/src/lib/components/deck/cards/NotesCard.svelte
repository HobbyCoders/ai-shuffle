<script lang="ts">
	import { layout } from '$lib/stores/layout.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * NotesCard - Global notes for ideas, bugs, planning
	 *
	 * Provides list view with search/filter, and editor view for editing notes.
	 */

	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import NoteFileAutocomplete from './NoteFileAutocomplete.svelte';
	import { resizeToFitContent } from './notes-textarea-resize';
	import './card-design-system.css';
	import { StickyNote, Search as SearchIcon, Plus, Pin, Trash2, Paperclip, X } from 'lucide-svelte';
	import {
		getNotes,
		getNote,
		createNote,
		updateNote,
		deleteNote,
		getNoteTags,
		getNoteAttachments,
		uploadNoteAttachment,
		deleteNoteAttachment,
		noteAttachmentDownloadUrl,
		type Note,
		type NoteTag,
		type NoteAttachment
	} from '$lib/api/client';
	import { onMount, untrack } from 'svelte';
	import { sanitizeHtml } from '$lib/utils/sanitize';

	interface Props {
		mobile?: boolean;
		initialItemId?: string | null;
	}

	let { mobile = false, initialItemId }: Props = $props();
	let consumedItemId: string | null = null;

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

	// Attachments state
	let attachments = $state<NoteAttachment[]>([]);
	let uploading = $state(false);
	let isDragOver = $state(false);
	let fileInputEl = $state<HTMLInputElement | null>(null);

	// `@` autocomplete state for referencing attached files
	let autocompleteRef = $state<ReturnType<typeof NoteFileAutocomplete> | null>(null);
	let atQuery = $state<string | null>(null); // text typed after `@`, or null when inactive
	let atStartIndex = $state(-1); // index of the `@` character in editorContent
	let autocompleteVisible = $derived(atQuery !== null && (editingNote !== null || isCreatingNew));

	// The note id we can attach files to (created notes only)
	let currentNoteId = $derived(editingNote?.id ?? null);

	// Side-by-side list grouping.
	const pinnedNotes = $derived(notes.filter((n) => n.is_pinned));
	const recentNotes = $derived(notes.filter((n) => !n.is_pinned));

	// Debounce for auto-save
	let saveTimeout: ReturnType<typeof setTimeout> | null = null;
	let hasUnsavedChanges = $state(false);
	let editorGeneration = 0;
	let editRevision = 0;
	let navigationRequest = 0;
	let savePromise: Promise<void> | null = null;
	let destroyed = false;
	const DRAFT_KEY = 'ai-shuffle-note-draft';

	function persistDraft() {
		try {
			if (!hasUnsavedChanges) sessionStorage.removeItem(DRAFT_KEY);
			else sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
				note: editingNote, isCreatingNew, title: editorTitle, content: editorContent,
				tags: editorTags, pinned: editorPinned
			}));
		} catch { /* Storage failures must not interrupt editing. */ }
	}

	// Conflicting/deleted server notes become independent drafts so neither
	// the local text nor the newer server version can be overwritten.
	async function restoreDraft() {
		let draft: { note?: Note | null; title: string; content: string; tags: string[]; pinned?: boolean };
		try {
			const raw = sessionStorage.getItem(DRAFT_KEY);
			if (!raw) return;
			draft = JSON.parse(raw);
		} catch { return; /* Leave corrupt/unavailable storage alone. */ }
		if (typeof draft?.title !== 'string' || typeof draft.content !== 'string' || !Array.isArray(draft.tags)) return;
		const generation = editorGeneration;
		if (draft.note) {
			let current: Note | null;
			try {
				current = await getNote(draft.note.id);
			} catch (error) {
				if ((error as { status?: number }).status !== 404) return;
				current = null;
			}
			if (generation !== editorGeneration) return;
			if (!current || current.updated_at !== draft.note.updated_at) {
				draft.note = null;
				remoteChange = 'This note changed elsewhere. Your recovered text is kept as a new draft.';
			}
		}
		editingNote = draft.note ?? null;
		isCreatingNew = !editingNote;
		editorTitle = draft.title;
		editorContent = draft.content;
		editorTags = draft.tags;
		editorPinned = !!draft.pinned;
		hasUnsavedChanges = true;
		persistDraft();
		if (editingNote) void loadAttachments(editingNote.id);
	}

	function protectDraft(event: BeforeUnloadEvent) {
		if (!hasUnsavedChanges && !saving) return;
		persistDraft();
		event.preventDefault();
		event.returnValue = '';
	}

	// Save-status label for the editor top bar.
	const saveLabel = $derived(saving ? 'Saving…' : hasUnsavedChanges ? 'Unsaved' : 'Saved');

	// A pending confirmation awaiting the user (replaces native confirm()).
	let pendingConfirm = $state<{
		title: string;
		message: string;
		confirmLabel: string;
		danger: boolean;
		action: () => void;
	} | null>(null);

	function runPendingConfirm() {
		const action = pendingConfirm?.action;
		pendingConfirm = null;
		action?.();
	}

	// Load notes and tags
	async function loadData(quiet = false) {
		if (!quiet) loading = true;
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

	// Re-read the open note from the single-note endpoint, which reconciles the
	// note against its on-disk markdown file. Adopt newer content into the editor
	// only when there are no unsaved local edits, so an external file edit shows
	// up without clobbering in-progress work.
	async function refreshOpenNote() {
		if (!editingNote || hasUnsavedChanges || saving) return;
		const id = editingNote.id;
		const generation = editorGeneration;
		const revision = editRevision;
		try {
			const fresh = await getNote(id);
			if (generation !== editorGeneration || revision !== editRevision || hasUnsavedChanges || saving) return;
			if (JSON.stringify(fresh) !== JSON.stringify(editingNote)) {
				editingNote = fresh;
				editorTitle = fresh.title;
				editorContent = fresh.content;
				editorTags = [...fresh.tags];
				editorPinned = fresh.is_pinned;
			}
		} catch {
			// Best-effort refresh; leave the editor as-is on failure.
		}
	}

	// Refresh on tab/window focus. Notes are materialized as markdown files that
	// may be edited outside the app; the single-note GET reconciles those edits,
	// so refresh the list and re-read the open note. Skips while editing/uploading.
	function handleWindowFocus() {
		if (saving || uploading || hasUnsavedChanges) return;
		loadData();
		refreshOpenNote();
	}

	// visibilitychange covers switching back to this tab within the same browser
	// window — the window 'focus' event doesn't always fire for tab switches.
	function handleVisibilityChange() {
		if (document.visibilityState === 'visible') {
			handleWindowFocus();
		}
	}

	// Load data on mount
	onMount(() => {
		void restoreDraft().finally(() => { draftRestored = true; });
		loadData();
		window.addEventListener('beforeunload', protectDraft);
		window.addEventListener('focus', handleWindowFocus);
		document.addEventListener('visibilitychange', handleVisibilityChange);
		return () => {
			destroyed = true;
			persistDraft();
			window.removeEventListener('beforeunload', protectDraft);
			window.removeEventListener('focus', handleWindowFocus);
			document.removeEventListener('visibilitychange', handleVisibilityChange);
			if (saveTimeout) {
				clearTimeout(saveTimeout);
			}
		};
	});

	// Reload the list when search/filter changes. In the side-by-side layout the
	// list stays visible while editing, so filtering must work at all times.
	//
	// The FIRST run is skipped on purpose (R14): an effect always runs once on
	// mount, so with onMount's load this fired a second, 300 ms-delayed
	// /notes request for the identical (empty) filter — two spinners and two
	// round trips every time Notes was opened. Only real filter *changes*
	// should refetch.
	let filterEffectPrimed = false;
	$effect(() => {
		// Access these to establish reactivity
		searchQuery;
		selectedTag;
		if (!filterEffectPrimed) {
			filterEffectPrimed = true;
			return;
		}
		const timeout = setTimeout(loadData, 300);
		return () => clearTimeout(timeout);
	});

	function cancelAutosave() {
		if (saveTimeout) clearTimeout(saveTimeout);
		saveTimeout = null;
	}

	// All editor navigation shares the same save/discard decision. A named note
	// is flushed before moving on; an untitled or failed draft requires consent.
	async function changeEditor(action: () => void) {
		const request = ++navigationRequest;
		cancelAutosave();
		if (saving || (hasUnsavedChanges && editorTitle.trim())) await saveNote();
		if (request !== navigationRequest) return;
		const apply = () => {
			cancelAutosave();
			editorGeneration++;
			hasUnsavedChanges = false;
			uploading = false;
			persistDraft();
			action();
		};
		if (hasUnsavedChanges) {
			pendingConfirm = {
				title: 'Discard changes',
				message: 'You have unsaved changes. Discard them?',
				confirmLabel: 'Discard', danger: true, action: apply
			};
			return;
		}
		apply();
	}

	function startNewNote() {
		void changeEditor(resetNewNote);
	}

	function resetNewNote() {
		isCreatingNew = true;
		editingNote = null;
		editorTitle = '';
		editorContent = '';
		editorTags = [];
		editorPinned = false;
		hasUnsavedChanges = false;
		attachments = [];
		showPreview = false;
		closeAutocomplete();
	}

	let draftRestored = $state(false);
    $effect(() => {
        const id = initialItemId;
        if (!id) { consumedItemId = null; return; }
        if (!draftRestored || id === consumedItemId) return;
        consumedItemId = id;
        let cancelled = false;
        getNote(id).then(note => {
            if (!cancelled) untrack(() => { openNote(note); layout.clearFeatureItemId(); });
        }).catch(() => { if (!cancelled) error = 'Could not open the selected note.'; });
        return () => { cancelled = true; };
    });

	// Open a note for editing
	function openNote(note: Note) {
		if (editingNote?.id === note.id) return;
		void changeEditor(() => selectNote(notes.find((n) => n.id === note.id) ?? note));
	}

	function selectNote(note: Note) {
		editingNote = note;
		isCreatingNew = false;
		editorTitle = note.title;
		editorContent = note.content;
		editorTags = [...note.tags];
		editorPinned = note.is_pinned;
		hasUnsavedChanges = false;
		attachments = [];
		showPreview = false;
		closeAutocomplete();
		loadAttachments(note.id);
		// The list comes from the non-reconciling endpoint, so it may hold stale
		// content when the .md file was edited outside the app. Re-read through the
		// single-note GET (which reconciles against disk) and adopt any newer
		// content into the just-opened editor.
		refreshOpenNote();
	}

	// Load attachments for a note
	async function loadAttachments(noteId: string) {
		const generation = editorGeneration;
		try {
			const loaded = await getNoteAttachments(noteId);
			if (generation === editorGeneration && editingNote?.id === noteId) attachments = loaded;
		} catch (e) {
			// Non-fatal: editor still works without the attachment list
			if (generation === editorGeneration && editingNote?.id === noteId) attachments = [];
		}
	}

	// Close editor and go back to list
	function closeEditor() {
		void changeEditor(doCloseEditor);
	}

	function doCloseEditor() {
		editingNote = null;
		isCreatingNew = false;
		attachments = [];
		closeAutocomplete();
		loadData();
	}

	// Save the current note (create or update)
	function saveNote(): Promise<void> {
		cancelAutosave();
		if (savePromise) return savePromise;
		if (!editorTitle.trim() || (!isCreatingNew && !editingNote)) return Promise.resolve();
		const generation = editorGeneration;
		saving = true;
		savePromise = (async () => {
			try {
				do {
					const revision = editRevision;
					const data = { title: editorTitle.trim(), content: editorContent, tags: [...editorTags], is_pinned: editorPinned };
					const saved = isCreatingNew ? await createNote(data) : await updateNote(editingNote!.id, data);
					const idx = notes.findIndex((n) => n.id === saved.id);
					if (idx >= 0) notes[idx] = saved;
					else notes = [saved, ...notes];
					if (generation !== editorGeneration) return;
					isCreatingNew = false;
					editingNote = saved;
					hasUnsavedChanges = revision !== editRevision;
					persistDraft();
				} while (hasUnsavedChanges && editorTitle.trim());
			} catch (e) {
				error = e instanceof Error ? e.message : 'Failed to save note';
			} finally {
				saving = false;
				savePromise = null;
			}
		})();
		return savePromise;
	}

	// Delete the current note
	function handleDelete() {
		if (!editingNote) return;
		pendingConfirm = {
			title: 'Delete note',
			message: 'Delete this note? This cannot be undone.',
			confirmLabel: 'Delete',
			danger: true,
			action: doDelete
		};
	}

	async function doDelete() {
		if (!editingNote) return;
		cancelAutosave();
		const id = editingNote.id;
		const generation = editorGeneration;
		if (savePromise) await savePromise;
		try {
			await deleteNote(id);
			if (generation === editorGeneration) {
				editorGeneration++;
				editingNote = null;
				isCreatingNew = false;
				hasUnsavedChanges = false;
				persistDraft();
			}
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

	// Delete a note from list view
	function handleDeleteFromList(note: Note, e: Event) {
		e.stopPropagation();
		pendingConfirm = {
			title: 'Delete note',
			message: 'Delete this note? This cannot be undone.',
			confirmLabel: 'Delete',
			danger: true,
			action: () => doDeleteFromList(note)
		};
	}

	async function doDeleteFromList(note: Note) {
		try {
			await deleteNote(note.id);
			loadData();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete note';
		}
	}

	// Auto-resize textarea to fit content (desktop only)
	function autoResizeTextarea() {
		if (!textareaEl) return;
		// Skip auto-resize on mobile/touch — let the textarea scroll internally
		// to prevent the view from jumping when the virtual keyboard is open
		if (mobile || window.matchMedia('(pointer: coarse)').matches) return;
		// Resize while preserving the editor column's scroll position, so typing
		// past the viewport doesn't yank the view to the top (see helper docs).
		const scroller = textareaEl.closest('.ns-editorcol') as HTMLElement | null;
		resizeToFitContent(textareaEl, scroller);
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
		editRevision++;
		hasUnsavedChanges = true;
		// Svelte bindings update after input handlers; capture the resulting draft.
		queueMicrotask(() => {
			if (destroyed) return;
			persistDraft();
			cancelAutosave();
			if (hasUnsavedChanges && editorTitle.trim()) saveTimeout = setTimeout(() => void saveNote(), 1000);
		});
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

	// ========================================================================
	// Attachments
	// ========================================================================

	// Ensure the note exists (has an id) so files can be associated with it.
	// New, unsaved notes are persisted first (a title is required to save).
	async function ensureNoteSaved(): Promise<string | null> {
		const existing = editingNote;
		if (existing) return existing.id;
		if (isCreatingNew) {
			if (!editorTitle.trim()) {
				error = 'Add a title before attaching files';
				return null;
			}
			// Cancel any pending autosave to avoid a duplicate create
			if (saveTimeout) {
				clearTimeout(saveTimeout);
				saveTimeout = null;
			}
			await saveNote();
			// saveNote() assigns editingNote on success; re-read it.
			const created = editingNote as Note | null;
			return created?.id ?? null;
		}
		return null;
	}

	function triggerFilePicker() {
		fileInputEl?.click();
	}

	async function handleFileInputChange(e: Event) {
		const input = e.target as HTMLInputElement;
		if (input.files && input.files.length > 0) {
			await uploadFiles(Array.from(input.files));
		}
		// Reset so selecting the same file again re-fires the change event
		input.value = '';
	}

	async function uploadFiles(files: File[]) {
		const generation = editorGeneration;
		const noteId = await ensureNoteSaved();
		if (!noteId || generation !== editorGeneration) return;

		uploading = true;
		error = null;
		try {
			for (const file of files) {
				const attachment = await uploadNoteAttachment(noteId, file);
				if (generation !== editorGeneration) return;
				attachments = [...attachments, attachment];
			}
		} catch (e) {
			if (generation === editorGeneration) error = e instanceof Error ? e.message : 'Failed to upload file';
		} finally {
			if (generation === editorGeneration) uploading = false;
		}
	}

	async function removeAttachment(attachment: NoteAttachment) {
		if (!editingNote) return;
		const generation = editorGeneration;
		try {
			await deleteNoteAttachment(editingNote.id, attachment.id);
			if (generation !== editorGeneration) return;
			attachments = attachments.filter(a => a.id !== attachment.id);
		} catch (e) {
			if (generation === editorGeneration) error = e instanceof Error ? e.message : 'Failed to remove attachment';
		}
	}

	// Drag-and-drop upload onto the editor content area
	function handleDragOver(e: DragEvent) {
		e.preventDefault();
		isDragOver = true;
	}

	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		isDragOver = false;
	}

	async function handleDrop(e: DragEvent) {
		e.preventDefault();
		isDragOver = false;
		const dropped = e.dataTransfer?.files;
		if (dropped && dropped.length > 0) {
			await uploadFiles(Array.from(dropped));
		}
	}

	// ========================================================================
	// `@` reference autocomplete
	// ========================================================================

	// Find the active `@` mention immediately before the caret. The `@` must be
	// at the start of input or preceded by whitespace, and the query (text after
	// `@`) must not contain whitespace.
	function detectAtMention() {
		if (!textareaEl) {
			closeAutocomplete();
			return;
		}
		const caret = textareaEl.selectionStart ?? editorContent.length;
		const before = editorContent.slice(0, caret);
		const atIdx = before.lastIndexOf('@');
		if (atIdx === -1) {
			closeAutocomplete();
			return;
		}
		// `@` must be at start or preceded by whitespace
		if (atIdx > 0 && !/\s/.test(before[atIdx - 1])) {
			closeAutocomplete();
			return;
		}
		const query = before.slice(atIdx + 1);
		if (/\s/.test(query)) {
			closeAutocomplete();
			return;
		}
		atStartIndex = atIdx;
		atQuery = query;
	}

	function closeAutocomplete() {
		atQuery = null;
		atStartIndex = -1;
	}

	// Insert a markdown reference to the chosen attachment, replacing the typed
	// `@query`. The token renders as a clickable link in preview.
	function insertAttachmentReference(attachment: NoteAttachment) {
		if (!textareaEl || atStartIndex < 0) {
			closeAutocomplete();
			return;
		}
		const caret = textareaEl.selectionStart ?? editorContent.length;
		const token = `@[${attachment.filename}](attachment://${attachment.id}) `;
		const newContent =
			editorContent.slice(0, atStartIndex) + token + editorContent.slice(caret);
		editorContent = newContent;
		closeAutocomplete();
		markDirty();

		// Restore caret position just after the inserted token
		const newCaret = atStartIndex + token.length;
		requestAnimationFrame(() => {
			if (textareaEl) {
				textareaEl.focus();
				textareaEl.setSelectionRange(newCaret, newCaret);
				autoResizeTextarea();
			}
		});
	}

	// Forward navigation keys to the autocomplete when it's open
	function handleTextareaKeydown(e: KeyboardEvent) {
		if (autocompleteVisible && autocompleteRef) {
			autocompleteRef.handleKeyDown(e);
		}
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

	function escapeHtml(text: string): string {
		return text
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;');
	}

	// Convert an `@[name](attachment://id)` reference into a clickable chip link.
	// Resolves to the real download URL when the referenced attachment still
	// exists; otherwise renders the name as plain text so dangling refs degrade
	// gracefully.
	function renderAttachmentReferences(content: string): string {
		return content.replace(
			/@\[([^\]]+)\]\(attachment:\/\/([A-Za-z0-9_-]+)\)/g,
			(_match, rawName: string, attachmentId: string) => {
				const name = escapeHtml(rawName);
				const att = attachments.find(a => a.id === attachmentId);
				if (!att || !currentNoteId) {
					return `<span class="note-ref note-ref--missing">@${name}</span>`;
				}
				const url = noteAttachmentDownloadUrl(currentNoteId, attachmentId);
				return `<a class="note-ref" href="${url}" target="_blank" rel="noopener noreferrer" title="${name}">@${name}</a>`;
			}
		);
	}

	// Simple markdown to HTML (basic conversion)
	function renderMarkdown(content: string): string {
		return renderAttachmentReferences(content)
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
	let remoteChange = $state('');
	watchResources(['notes'], async () => {
		const selected = editingNote;
		const revision = editRevision;
		await loadData(true);
		if (!selected || editingNote?.id !== selected.id) return;
		try {
			const fresh = await getNote(selected.id);
			if (editingNote?.id !== selected.id) return;
			if (hasUnsavedChanges || saving || saveTimeout || savePromise || revision !== editRevision) {
				if (JSON.stringify(fresh) !== JSON.stringify(selected)) remoteChange = 'This note changed elsewhere. Your unsaved text is kept here.';
				return;
			}
			editingNote = fresh;
			editorTitle = fresh.title;
			editorContent = fresh.content;
			editorTags = [...fresh.tags];
			editorPinned = fresh.is_pinned;
			remoteChange = '';
			await loadAttachments(fresh.id);
		} catch (e: any) {
			if (editingNote?.id !== selected.id || (e.status !== 404 && e.status_code !== 404)) return;
			if (hasUnsavedChanges || saving || saveTimeout || savePromise || revision !== editRevision) {
				cancelAutosave();
				// Convert the recovered buffer to a new note rather than PUT a deleted id.
				editingNote = null;
				isCreatingNew = true;
				persistDraft();
				remoteChange = 'This note was deleted elsewhere. Your text is kept as a new draft.';
			} else {
				editingNote = null;
				editorTitle = '';
				editorContent = '';
				remoteChange = 'This note was deleted elsewhere.';
			}
		}
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

{#snippet noteItem(note: Note)}
	<button class="ns-item" class:on={editingNote?.id === note.id} onclick={() => openNote(note)}>
		<h4 class="ns-item-title">
			{#if note.is_pinned}<span class="ns-item-pin"><Pin size={12} /></span>{/if}
			<span class="ns-item-title-text">{note.title || 'Untitled'}</span>
		</h4>
		{#if note.content}<p class="ns-item-preview">{note.content}</p>{/if}
		<div class="ns-item-foot">
			{#each note.tags.slice(0, 3) as tag (tag)}<span class="ns-chip ns-chip--sm">{tag}</span>{/each}
			<span class="ns-item-date">{formatRelativeTime(note.updated_at)}</span>
		</div>
	</button>
{/snippet}

<div class="ns" class:editing={!!(editingNote || isCreatingNew)}>
	<div class="ns-wrap">
		<div class="ns-head">
			<StudioPageHeader eyebrow="Global · across all projects" title="" accent="Notes">
				{#snippet actions()}
					<button class="ns-btn ns-btn--primary" onclick={startNewNote}>
						<Plus size={15} strokeWidth={2} /><span>New note</span>
					</button>
				{/snippet}
			</StudioPageHeader>
		</div>

		<div class="ns-layout">
			<!-- LEFT: note list -->
			<aside class="ns-listcol">
				<div class="ns-search">
					<SearchIcon size={14} strokeWidth={1.8} />
					<input type="text" placeholder="Search notes…" bind:value={searchQuery} />
				</div>
				<div class="ns-tagbar">
					<button class="ns-tag" class:on={selectedTag === null} onclick={() => (selectedTag = null)}>all</button>
					{#each tags as tag (tag.name)}
						<button class="ns-tag" class:on={selectedTag === tag.name} onclick={() => (selectedTag = selectedTag === tag.name ? null : tag.name)}>
							{tag.name}<span class="ns-tag-count">{tag.count}</span>
						</button>
					{/each}
				</div>
				<div class="ns-scroll">
					{#if loading}
						<div class="ns-liststate">Loading notes…</div>
					{:else if error}
						<div class="ns-liststate ns-liststate--err">{error}</div>
					{:else if notes.length === 0}
						<div class="ns-listempty">
							<StickyNote size={28} strokeWidth={1.25} />
							<p>{searchQuery || selectedTag ? 'No notes match' : 'No notes yet'}</p>
							<button class="ns-link" onclick={startNewNote}>Create your first note</button>
						</div>
					{:else}
						{#if pinnedNotes.length > 0}
							<div class="ns-label">Pinned</div>
							{#each pinnedNotes as note (note.id)}{@render noteItem(note)}{/each}
						{/if}
						{#if recentNotes.length > 0}
							<div class="ns-label">{pinnedNotes.length ? 'Recent' : 'All notes'}</div>
							{#each recentNotes as note (note.id)}{@render noteItem(note)}{/each}
						{/if}
					{/if}
				</div>
			</aside>

			<!-- RIGHT: editor -->
			<section class="ns-editorcol">
				{#if editingNote || isCreatingNew}
					<div class="ns-etop">
						<span class="ns-etop-label">Markdown</span>
						<div class="ns-etop-right">
							<span class="ns-save"><span class="ns-save-dot" class:dirty={hasUnsavedChanges} class:busy={saving || uploading}></span>{uploading ? 'Uploading…' : saveLabel}</span>
							<button class="ns-iconbtn" title="Attach file" onclick={triggerFilePicker} disabled={uploading}><Paperclip size={15} /></button>
							<button class="ns-iconbtn" class:on={editorPinned} title={editorPinned ? 'Unpin' : 'Pin'} onclick={() => { editorPinned = !editorPinned; markDirty(); }}><Pin size={15} /></button>
							<button class="ns-iconbtn ns-iconbtn--danger" title="Delete note" onclick={handleDelete} disabled={isCreatingNew}><Trash2 size={15} /></button>
							<button class="ns-iconbtn" title="Close" onclick={closeEditor}><X size={16} /></button>
						</div>
					</div>
					<input type="file" multiple bind:this={fileInputEl} onchange={handleFileInputChange} style="display:none" />

					<input class="ns-title-input" placeholder="Note title…" bind:value={editorTitle} oninput={markDirty} />

					<div class="ns-etags">
						{#each editorTags as tag (tag)}
							<span class="ns-chip ns-chip--edit">{tag}<button class="ns-chip-x" onclick={() => removeTag(tag)} aria-label="Remove tag"><X size={11} /></button></span>
						{/each}
						<input class="ns-tag-input" placeholder="add tag…" bind:value={newTagInput} onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }} />
					</div>

					{#if attachments.length > 0}
						<div class="ns-attachbar">
							{#each attachments as att (att.id)}
								<div class="ns-attach">
									{#if currentNoteId}
										<a class="ns-attach-link" href={noteAttachmentDownloadUrl(currentNoteId, att.id)} target="_blank" rel="noopener noreferrer" title={`Download ${att.filename}`}>
											<Paperclip size={12} /><span class="ns-attach-name">{att.filename}</span>
										</a>
									{:else}
										<span class="ns-attach-link"><Paperclip size={12} /><span class="ns-attach-name">{att.filename}</span></span>
									{/if}
									<button class="ns-attach-x" onclick={() => removeAttachment(att)} title="Remove attachment"><X size={12} /></button>
								</div>
							{/each}
						</div>
					{/if}

					<div class="ns-tabs">
						<button class:active={!showPreview} onclick={() => (showPreview = false)}>Edit</button>
						<button class:active={showPreview} onclick={() => (showPreview = true)}>Preview</button>
					</div>

					<div
						class="ns-content"
						class:drag-over={isDragOver}
						role="region"
						aria-label="Note content"
						ondragover={handleDragOver}
						ondragleave={handleDragLeave}
						ondrop={handleDrop}
					>
						{#if showPreview}
							<div class="ns-preview md">
								{#if editorContent}{@html sanitizeHtml(renderMarkdown(editorContent))}{:else}<p class="ns-preview-empty">Nothing to preview</p>{/if}
							</div>
						{:else}
							<div class="ns-ta-wrap">
								<textarea
									class="ns-textarea"
									placeholder="Write your note in Markdown… Type @ to reference an attached file."
									bind:value={editorContent}
									bind:this={textareaEl}
									oninput={() => { markDirty(); autoResizeTextarea(); detectAtMention(); }}
									onkeydown={handleTextareaKeydown}
									onclick={detectAtMention}
									onkeyup={detectAtMention}
									onblur={() => requestAnimationFrame(closeAutocomplete)}
								></textarea>
								<NoteFileAutocomplete
									bind:this={autocompleteRef}
									visible={autocompleteVisible}
									query={atQuery ?? ''}
									{attachments}
									onSelect={insertAttachmentReference}
									onClose={closeAutocomplete}
								/>
							</div>
						{/if}
						{#if isDragOver}<div class="ns-drop">Drop files to attach</div>{/if}
					</div>
				{:else}
					<div class="ns-emptyeditor">
						<StickyNote size={40} strokeWidth={1.1} />
						<p class="ns-emptyeditor-title">Select a note</p>
						<p class="ns-emptyeditor-sub">Pick a note on the left, or start a new one.</p>
						<button class="ns-btn ns-btn--primary" onclick={startNewNote}><Plus size={15} strokeWidth={2} /><span>New note</span></button>
					</div>
				{/if}
			</section>
		</div>
	</div>
</div>

<!-- Confirmation dialog (delete / discard) -->
<ConfirmDialog
	open={!!pendingConfirm}
	title={pendingConfirm?.title ?? 'Confirm'}
	message={pendingConfirm?.message ?? ''}
	confirmLabel={pendingConfirm?.confirmLabel ?? 'Confirm'}
	danger={pendingConfirm?.danger ?? false}
	onConfirm={runPendingConfirm}
	onCancel={() => (pendingConfirm = null)}
/>

<style>
	.ns {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	/* Centered, max-width container — matches the mockup .wrap (not full-bleed). */
	.ns-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.ns-head { flex-shrink: 0; padding: 22px 28px 18px; }

	.ns-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, filter 120ms ease;
	}
	.ns-btn:hover { background: var(--hover-overlay); }
	.ns-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.ns-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }

	/* Two-column layout */
	.ns-layout {
		flex: 1; min-height: 0;
		display: grid; grid-template-columns: 320px 1fr; gap: 18px;
		padding: 0 28px 28px; overflow: hidden; align-items: stretch;
	}

	/* Left: note list */
	.ns-listcol {
		min-height: 0; display: flex; flex-direction: column;
		background: var(--card); border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem); overflow: hidden;
	}
	.ns-search {
		display: flex; align-items: center; gap: 8px; padding: 11px 13px;
		border-bottom: 1px solid var(--border-subtle, var(--border)); flex-shrink: 0;
	}
	.ns-search :global(svg) { color: var(--muted-foreground); flex-shrink: 0; }
	.ns-search input {
		flex: 1; min-width: 0; background: transparent; border: none; outline: none;
		color: var(--foreground); font-size: 0.8125rem;
	}
	.ns-search input::placeholder { color: var(--muted-foreground); }

	.ns-tagbar {
		display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 13px;
		border-bottom: 1px solid var(--border-subtle, var(--border)); flex-shrink: 0;
	}
	.ns-tag {
		display: inline-flex; align-items: center; gap: 5px;
		font-family: var(--font-mono, monospace); font-size: 0.66rem; font-weight: 500;
		padding: 3px 8px; border-radius: 6px; border: none; cursor: pointer;
		background: color-mix(in oklch, var(--muted) 45%, transparent); color: var(--muted-foreground);
		transition: background-color 110ms ease, color 110ms ease;
	}
	.ns-tag:hover { color: var(--foreground); }
	.ns-tag.on { background: var(--primary); color: var(--primary-foreground); }
	.ns-tag-count { opacity: 0.7; }

	.ns-scroll { flex: 1; min-height: 0; overflow-y: auto; }
	.ns-label {
		font-family: var(--font-mono, monospace); font-size: 0.625rem; letter-spacing: 0.1em;
		text-transform: uppercase; color: var(--muted-foreground); font-weight: 600;
		padding: 11px 14px 5px;
	}

	.ns-item {
		display: block; width: 100%; text-align: left; cursor: pointer;
		padding: 11px 14px; border: none; background: transparent;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		border-left: 3px solid transparent;
		transition: background-color 110ms ease;
	}
	.ns-item:hover { background: var(--hover-overlay); }
	.ns-item.on {
		background: color-mix(in oklch, var(--primary) 14%, var(--card));
		border-left-color: var(--primary);
	}
	.ns-item-title {
		display: flex; align-items: center; gap: 6px; margin: 0 0 4px;
		font-family: var(--font-display, Georgia, serif); font-weight: 700; font-size: 0.875rem;
		color: var(--foreground);
	}
	.ns-item-pin { color: var(--warning, #d97706); display: inline-flex; flex-shrink: 0; }
	.ns-item-title-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ns-item-preview {
		margin: 0 0 7px; font-size: 0.75rem; color: var(--muted-foreground); line-height: 1.45;
		display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
	}
	.ns-item-foot { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
	.ns-item-date { font-family: var(--font-mono, monospace); font-size: 0.625rem; color: var(--muted-foreground); margin-left: auto; }

	.ns-chip {
		font-family: var(--font-mono, monospace); font-size: 0.6rem; font-weight: 500;
		padding: 2px 7px; border-radius: 6px;
		background: color-mix(in oklch, var(--muted) 45%, transparent); color: var(--muted-foreground);
	}
	.ns-chip--edit { display: inline-flex; align-items: center; gap: 4px; font-size: 0.68rem; padding: 3px 4px 3px 9px; }
	.ns-chip-x { display: grid; place-items: center; background: transparent; border: none; color: inherit; cursor: pointer; padding: 1px; border-radius: 4px; opacity: 0.7; }
	.ns-chip-x:hover { opacity: 1; background: var(--hover-overlay); }

	.ns-liststate, .ns-listempty { padding: 32px 16px; text-align: center; color: var(--muted-foreground); font-size: 0.8rem; }
	.ns-liststate--err { color: var(--destructive); }
	.ns-listempty { display: flex; flex-direction: column; align-items: center; gap: 8px; }
	.ns-listempty :global(svg) { opacity: 0.6; }
	.ns-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; }
	.ns-link:hover { text-decoration: underline; }

	/* Right: editor */
	.ns-editorcol {
		min-height: 0; display: flex; flex-direction: column; overflow-y: auto;
		background: var(--card); border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem); padding: 18px 22px 22px;
	}
	.ns-etop { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
	.ns-etop-label { font-family: var(--font-mono, monospace); font-size: 0.625rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted-foreground); }
	.ns-etop-right { display: flex; align-items: center; gap: 4px; }
	.ns-save { display: flex; align-items: center; gap: 6px; font-size: 0.7rem; color: var(--muted-foreground); margin-right: 6px; }
	.ns-save-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success, #22c55e); transition: background 150ms ease; }
	.ns-save-dot.dirty { background: var(--warning, #d97706); }
	.ns-save-dot.busy { background: var(--muted-foreground); animation: ns-pulse 1s ease-in-out infinite; }
	@keyframes ns-pulse { 50% { opacity: 0.35; } }

	.ns-iconbtn {
		display: grid; place-items: center; width: 28px; height: 28px;
		background: transparent; border: none; border-radius: 7px;
		color: var(--muted-foreground); cursor: pointer; transition: color 110ms ease, background-color 110ms ease;
	}
	.ns-iconbtn:hover { color: var(--foreground); background: var(--hover-overlay); }
	.ns-iconbtn:disabled { opacity: 0.4; cursor: not-allowed; }
	.ns-iconbtn.on { color: var(--warning, #d97706); }
	.ns-iconbtn--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	.ns-title-input {
		width: 100%; background: transparent; border: none; outline: none;
		font-family: var(--font-display, Georgia, serif); font-weight: 800; font-size: 1.5rem;
		letter-spacing: -0.02em; color: var(--foreground); margin-bottom: 10px;
	}
	.ns-title-input::placeholder { color: var(--muted-foreground); opacity: 0.6; }

	.ns-etags { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
	.ns-tag-input {
		min-width: 80px; flex: 1; background: transparent; border: none; outline: none;
		font-family: var(--font-mono, monospace); font-size: 0.7rem; color: var(--foreground);
	}
	.ns-tag-input::placeholder { color: var(--muted-foreground); }

	.ns-attachbar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
	.ns-attach { display: inline-flex; align-items: center; gap: 4px; padding: 3px 4px 3px 8px; border-radius: 7px; background: color-mix(in oklch, var(--muted) 40%, transparent); font-size: 0.72rem; }
	.ns-attach-link { display: inline-flex; align-items: center; gap: 5px; color: var(--foreground); text-decoration: none; }
	.ns-attach-link:hover { color: var(--primary); }
	.ns-attach-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; }
	.ns-attach-x { display: grid; place-items: center; background: transparent; border: none; color: var(--muted-foreground); cursor: pointer; padding: 1px; border-radius: 4px; }
	.ns-attach-x:hover { color: var(--destructive); }

	.ns-tabs { display: inline-flex; gap: 2px; padding: 3px; background: color-mix(in oklch, var(--muted) 40%, transparent); border-radius: 8px; width: fit-content; margin-bottom: 12px; }
	.ns-tabs button {
		padding: 4px 14px; border: none; background: transparent; border-radius: 6px;
		color: var(--muted-foreground); font-size: 0.78rem; font-weight: 500; cursor: pointer;
	}
	.ns-tabs button:hover { color: var(--foreground); }
	.ns-tabs button.active { background: var(--card); color: var(--foreground); }

	.ns-content { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.ns-content.drag-over { outline: 2px dashed var(--primary); outline-offset: 2px; border-radius: 8px; }
	.ns-ta-wrap { position: relative; flex: 1; min-height: 0; display: flex; }
	.ns-textarea {
		width: 100%; min-height: 240px; padding: 4px 0; background: transparent; border: none; outline: none;
		color: var(--foreground); font-family: var(--font-mono, monospace); font-size: 0.85rem; line-height: 1.65;
		resize: none; overflow: hidden;
	}
	.ns-textarea::placeholder { color: var(--muted-foreground); }
	@media (pointer: coarse), (max-width: 639px) {
		.ns-textarea { flex: 1; overflow-y: auto; }
	}

	.ns-drop {
		position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
		background: color-mix(in oklch, var(--primary) 10%, transparent); color: var(--primary);
		font-size: 0.9rem; font-weight: 600; border-radius: 8px; pointer-events: none;
	}

	.ns-emptyeditor {
		flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
		text-align: center; color: var(--muted-foreground);
	}
	.ns-emptyeditor :global(svg) { opacity: 0.45; }
	.ns-emptyeditor-title { margin: 4px 0 0; font-size: 1rem; font-weight: 600; color: var(--foreground); }
	.ns-emptyeditor-sub { margin: 0 0 8px; font-size: 0.82rem; }

	/* Rendered Markdown preview */
	.ns-preview { flex: 1; min-height: 0; overflow-y: auto; font-size: 0.9rem; line-height: 1.7; color: var(--foreground); }
	.ns-preview :global(h1), .ns-preview :global(h2), .ns-preview :global(h3) { font-family: var(--font-display, Georgia, serif); margin: 1.1em 0 0.5em; font-weight: 700; }
	.ns-preview :global(h1) { font-size: 1.5em; }
	.ns-preview :global(h2) { font-size: 1.3em; }
	.ns-preview :global(h3) { font-size: 1.12em; }
	.ns-preview :global(p) { margin: 0 0 0.8em; }
	.ns-preview :global(li) { margin: 0 0 0.4em 1.4em; }
	.ns-preview :global(pre) { padding: 12px; background: color-mix(in oklch, var(--muted) 40%, transparent); border-radius: 8px; overflow-x: auto; margin: 0 0 0.8em; }
	.ns-preview :global(code) { font-family: var(--font-mono, monospace); font-size: 0.82em; background: color-mix(in oklch, var(--muted) 45%, transparent); padding: 1px 5px; border-radius: 4px; }
	.ns-preview :global(pre code) { padding: 0; background: none; }
	.ns-preview :global(.note-ref) { display: inline-flex; align-items: center; padding: 0 6px; background: color-mix(in oklch, var(--primary) 14%, transparent); color: var(--primary); border-radius: 5px; text-decoration: none; font-size: 0.95em; }
	.ns-preview :global(.note-ref:hover) { background: color-mix(in oklch, var(--primary) 22%, transparent); }
	.ns-preview :global(.note-ref--missing) { background: color-mix(in oklch, var(--muted) 45%, transparent); color: var(--muted-foreground); }
	.ns-preview-empty { color: var(--muted-foreground); font-style: italic; }

	@media (max-width: 767px) {
		.ns-head { padding: 16px 16px 12px; }
		.ns-layout { grid-template-columns: 1fr; padding: 0 16px 18px; overflow: hidden; }

		/* One pane at a time: the list fills the screen; the editor takes over
		   full-screen when a note is open (mockup #page-notes). */
		.ns-listcol { max-height: none; border: none; border-radius: 0; background: transparent; }
		.ns-editorcol { display: none; }
		.ns.editing .ns-listcol { display: none; }
		.ns.editing .ns-editorcol { display: flex; flex: 1; min-height: 0; }

		/* Search → standalone pill; tag chips → horizontal scroll strip. */
		.ns-search { border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px); margin-bottom: 10px; }
		.ns-tagbar { border-bottom: none; padding: 2px 0 12px; flex-wrap: nowrap; overflow-x: auto; }
		.ns-tagbar::-webkit-scrollbar { display: none; }
		.ns-tagbar .ns-tag { flex: 0 0 auto; }

		/* Notes render as full-width cards (mockup .nitem). */
		.ns-item { border: 1px solid var(--border); border-radius: 12px; margin-bottom: 10px; }
		.ns-item.on { border-left-color: var(--primary); }
		.ns-label { padding: 6px 2px 8px; }
	}
</style>

