<script lang="ts">
	import { layout } from '$lib/stores/layout.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * NotesCard - Global notes for ideas, bugs, planning
	 *
	 * Provides list view with search/filter, and editor view for editing notes.
	 */

	import {
		ViewShell, ViewPanel, ViewMain, PanelSearch, PanelIconButton, ListItem,
		ListGroupLabel, DetailPane, EmptyDetail, SelectField, InlineBanner
	} from '$lib/components/layout/view';
	import { tick } from 'svelte';
	import { Marked } from 'marked';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import NoteFileAutocomplete from './NoteFileAutocomplete.svelte';
	import { resizeToFitContent } from './notes-textarea-resize';
	import { Notebook, Plus, Pin, Paperclip, X, Ellipsis } from 'lucide-svelte';
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
	import { registerReloadBlocker } from '$lib/services/app-update';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { escapeHtml } from '$lib/utils/escape-html';

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
	let moreOpen = $state(false);
	let moreButton = $state<HTMLButtonElement>();
	let moreMenu = $state<HTMLDivElement>();
	// Tags & files is a compact disclosure: it starts closed on every width so the
	// document sits directly under the tabs, and opens only when asked for.
	let fieldsOpen = $state(false);
	let narrow = $state(false);
	onMount(() => {
		const media = window.matchMedia('(max-width: 600px)');
		const sync = () => { narrow = media.matches; };
		sync();
		media.addEventListener('change', sync);
		return () => media.removeEventListener('change', sync);
	});
	async function toggleMore() {
		moreOpen = !moreOpen;
		if (moreOpen) { await tick(); moreMenu?.querySelector<HTMLButtonElement>('button')?.focus(); }
	}
	function closeMore(returnFocus = false) {
		moreOpen = false;
		if (returnFocus) moreButton?.focus();
	}
	function noteExcerpt(content: string): string {
		// A document heading is already represented by the note title. Keep it
		// only when it is the entire document, rather than running it into prose.
		const body = content.trimStart().replace(/^(?:#{1,6}[ \t]+[^\n]*(?:\n|$)|[^\n]+\n[ \t]*(?:=+|-+)[ \t]*(?:\n|$))[ \t\n]*/, '');
		return (body.trim() ? body : content)
			.replace(/```[^\n]*\n?([\s\S]*?)```/g, '$1')
			.replace(/!?\[([^\]]+)\]\([^)]*\)/g, '$1')
			.replace(/^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)/gm, '')
			.replace(/<[^>]*>/g, '').replace(/[*_`~]/g, '')
			.replace(/&(?:amp|lt|gt|quot|#39);/g, (entity) => ({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"})[entity] ?? entity)
			.replace(/\s+/g, ' ').trim();
	}
	let newTagInput = $state('');
	let textareaEl = $state<HTMLTextAreaElement | null>(null);

	// A title is one logical line, but may occupy any number of visual lines.
	function sizeTitle(node: HTMLTextAreaElement, _value: string) {
		const fit = () => {
			node.style.height = 'auto';
			node.style.height = `${node.scrollHeight}px`;
		};
		// jsdom test renders have no ResizeObserver; every supported browser does.
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
		observer?.observe(node);
		fit();
		return { update: fit, destroy: () => observer?.disconnect() };
	}

	function inputTitle(event: Event & { currentTarget: HTMLTextAreaElement }) {
		editorTitle = event.currentTarget.value.replace(/[\r\n]+/g, ' ');
		event.currentTarget.value = editorTitle;
		markDirty();
	}

	async function titleKeydown(event: KeyboardEvent) {
		if (event.key !== 'Enter' || event.isComposing) return;
		event.preventDefault();
		showPreview = false;
		await tick();
		textareaEl?.focus();
	}

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
	// What the detail shows, for its scroll reset: it changes only when a note
	// is opened, started or closed. A new note's first save gives it an id but
	// is still the same editor, so the writer keeps their place.
	let editorSession = $state(0);
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

	// An update never reloads the page over an unsaved or saving note (app-update.ts).
	onMount(() => registerReloadBlocker(() => (hasUnsavedChanges || saving ? 'note-unsaved' : null)));

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
			editorSession++;
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
		moreOpen = false;
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
		showPreview = !!note.content.trim();
		moreOpen = false;
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
		const scroller = textareaEl.closest('.st-scroll') as HTMLElement | null;
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

	// Marked's GFM task handling is the same parser used by chat. Notes keeps
	// its own instance: no chat code-block controls or interactive task toggles.
	const noteMarkdown = new Marked({ gfm: true });
	function renderMarkdown(content: string): string {
		return noteMarkdown.parse(renderAttachmentReferences(content), { async: false });
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

{#snippet metadata()}
	<span>{editingNote ? `Global · Updated ${formatRelativeTime(editingNote.updated_at)}` : 'Global · New note'}</span>
	<span class="note-save" role="status">{uploading ? 'Uploading…' : saveLabel}</span>
{/snippet}

{#snippet pinMark()}<Pin size={14} aria-hidden="true" />{/snippet}

{#snippet noteItem(note: Note)}
	<ListItem id={note.id} name={note.title || 'Untitled'}
		summary={[noteExcerpt(note.content), ...note.tags.slice(0, 3)].filter(Boolean).join(' · ')}
		meta={formatRelativeTime(note.updated_at)} icon={note.is_pinned ? Pin : Notebook}
		selected={editingNote?.id === note.id} onselect={() => openNote(note)} lead={note.is_pinned ? pinMark : undefined} />
{/snippet}

<svelte:window onclick={(event) => { if (!(event.target as Element)?.closest?.('.note-more')) closeMore(); }} onkeydown={(event) => { if (moreOpen && event.key === 'Escape') { event.preventDefault(); closeMore(true); } }} />

<ViewShell label="Notes" detailOpen={!!(editingNote || isCreatingNew)} detailKey={editorSession} onBack={closeEditor}>
	{#snippet panel()}
		<ViewPanel title="Notes" count={loading ? undefined : notes.length} {loading} listLabel="Notes list">
			{#snippet actions()}<PanelIconButton label="New note" icon={Plus} onclick={startNewNote} />{/snippet}
			{#snippet search()}<PanelSearch placeholder="Search notes" bind:value={searchQuery} />{/snippet}
			{#snippet filters()}
				<SelectField label="Filter by tag" value={selectedTag ?? ''}
					onchange={(value) => { selectedTag = value || null; }}
					options={[{ value: '', label: 'All tags' }, ...tags.map(tag => ({ value: tag.name, label: `${tag.name} (${tag.count})` }))]} />
			{/snippet}
			{#if loading}
				<p role="status">Loading notes…</p>
			{:else if error && !editingNote && !isCreatingNew}
				<InlineBanner tone="warn">{error}<button class="st-btn" onclick={() => loadData()}>Retry</button></InlineBanner>
			{:else if notes.length === 0}
				<EmptyDetail title={searchQuery || selectedTag ? 'No notes match' : 'No notes yet'}
					message={searchQuery || selectedTag ? 'Try another search or tag.' : 'Keep ideas, plans and files together.'}
					actionLabel={selectedTag ? 'Clear filters' : searchQuery ? 'Clear search' : undefined} onAction={() => { searchQuery = ''; selectedTag = null; }}>
					{#if !searchQuery && !selectedTag}
						<button class="st-btn primary" onclick={startNewNote}>Create your first note</button>
					{/if}
				</EmptyDetail>
			{:else}
				{#if pinnedNotes.length > 0}
					<ListGroupLabel compact label="Pinned" />
					{#each pinnedNotes as note (note.id)}{@render noteItem(note)}{/each}
				{/if}
				{#if recentNotes.length > 0}
					<ListGroupLabel compact label={pinnedNotes.length ? 'Recent' : 'All notes'} />
					{#each recentNotes as note (note.id)}{@render noteItem(note)}{/each}
				{/if}
			{/if}
		</ViewPanel>
	{/snippet}
	{#snippet main(back)}
		{#if editingNote || isCreatingNew}
			<DetailPane width="reading" name={editorTitle.trim() || 'New note'} backLabel="Notes" onBack={back} compactHeader={narrow} subtitle={narrow ? undefined : metadata}
				tabs={[{ id: 'edit', label: 'Edit' }, { id: 'preview', label: 'Preview' }]}
				tab={showPreview ? 'preview' : 'edit'} onTabChange={(tab) => { showPreview = tab === 'preview'; }}>
				{#snippet heading()}
					<h1 class="note-title"><span class="sr-only" aria-hidden="true">{editorTitle || 'New note'}</span><textarea aria-label="Note title" placeholder="New note" rows="1" value={editorTitle} use:sizeTitle={editorTitle} oninput={inputTitle} onkeydown={titleKeydown}></textarea></h1>
				{/snippet}
				{#snippet primary()}
					<button class="st-btn icon" title="Attach file" aria-label="Attach file" onclick={triggerFilePicker} disabled={uploading}><Paperclip /></button>
					<button class="st-btn icon" title={editorPinned ? 'Unpin' : 'Pin'} aria-label={editorPinned ? 'Unpin' : 'Pin'} aria-pressed={editorPinned} onclick={() => { editorPinned = !editorPinned; markDirty(); }}><Pin /></button>
					<div class="note-more" onfocusout={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) closeMore(); }}>
						<button class="st-btn icon" bind:this={moreButton} title="More" aria-label="More note actions" aria-haspopup="menu" aria-expanded={moreOpen} onclick={toggleMore}><Ellipsis /></button>
						{#if moreOpen}
							<div class="note-menu" role="menu" aria-label="Note actions" bind:this={moreMenu}>
								<button class="st-btn danger" role="menuitem" onclick={() => { closeMore(true); handleDelete(); }} disabled={isCreatingNew}>Delete note</button>
							</div>
						{/if}
					</div>
				{/snippet}
				{#if remoteChange}<InlineBanner>{remoteChange}</InlineBanner>{/if}
				{#if error}<InlineBanner tone="warn">{error}<button class="st-btn" onclick={() => { error = null; hasUnsavedChanges ? saveNote() : loadData(); }}>Retry</button></InlineBanner>{/if}
				<input type="file" multiple bind:this={fileInputEl} onchange={handleFileInputChange} hidden />
				<div class="note-writing">
				<details class="note-fields" bind:open={fieldsOpen}>
					<summary>Tags &amp; files{editorTags.length + attachments.length ? ` · ${editorTags.length + attachments.length}` : ''}</summary>
					<div class="note-fields-content">
					{#if narrow}<div class="note-metadata">{@render metadata()}</div>{/if}
					<div class="note-tags">
						{#each editorTags as tag (tag)}
							<button class="st-btn sm" onclick={() => removeTag(tag)} aria-label="Remove tag" title={tag}>{tag}<X /></button>
						{/each}
						<input class="note-tag-input" aria-label="Add tag" placeholder="add tag…" bind:value={newTagInput}
							onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }} />
					</div>
					{#if attachments.length > 0}
						<div class="note-attachments" aria-label="Note attachments">
							{#each attachments as att (att.id)}
								<div class="note-attachment">
									{#if currentNoteId}
										<a class="st-btn sm note-attachment-link" href={noteAttachmentDownloadUrl(currentNoteId, att.id)} target="_blank" rel="noopener noreferrer" title={`Download ${att.filename}`}><Paperclip /><span>{att.filename}</span></a>
									{:else}
										<span>{att.filename}</span>
									{/if}
									<button class="st-btn icon ghost" onclick={() => removeAttachment(att)} title="Remove attachment" aria-label="Remove attachment"><X /></button>
								</div>
							{/each}
						</div>
					{/if}
					</div>
				</details>
				<div class="note-content" class:drag-over={isDragOver} role="region" aria-label="Note content"
					ondragover={handleDragOver} ondragleave={handleDragLeave} ondrop={handleDrop}>
					{#if showPreview}
						<div class="note-preview prose">
							{#if editorContent}{@html sanitizeHtml(renderMarkdown(editorContent))}{:else}<p>Nothing to preview</p>{/if}
						</div>
					{:else}
						<div class="note-textarea-wrap">
							<textarea class="note-textarea" aria-label="Note body" placeholder="Write your note in Markdown… Type @ to reference an attached file."
								bind:value={editorContent} bind:this={textareaEl}
								oninput={() => { markDirty(); autoResizeTextarea(); detectAtMention(); }}
								onkeydown={handleTextareaKeydown} onclick={detectAtMention} onkeyup={detectAtMention}
								onblur={() => requestAnimationFrame(closeAutocomplete)}></textarea>
							<NoteFileAutocomplete bind:this={autocompleteRef} visible={autocompleteVisible} query={atQuery ?? ''}
								{attachments} onSelect={insertAttachmentReference} onClose={closeAutocomplete} />
						</div>
					{/if}
					{#if isDragOver}<div class="note-drop">Drop files to attach</div>{/if}
				</div>
				</div>
			</DetailPane>
		{:else}
			<ViewMain width="reading">
				{#if remoteChange}<InlineBanner>{remoteChange}</InlineBanner>{/if}
				<EmptyDetail title={notes.length ? 'Select a note' : 'No notes yet'} message={notes.length ? 'Pick a note from the list, or start a new one.' : 'Keep ideas, plans and files together.'} icon={Notebook}>
					<button class="st-btn primary" onclick={startNewNote}><Plus />New note</button>
				</EmptyDetail>
			</ViewMain>
		{/if}
	{/snippet}
</ViewShell>

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
	/* The shared kit owns all panel, row, header, tab and button geometry. */
	.note-save { font-size: 13px; color: var(--st-mfg); }
	.note-metadata { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; color: var(--st-mfg); font-size: 13px; }
	.note-title { width: 100%; min-width: 0; margin: 0; font-size: 24px; font-weight: 600; letter-spacing: -0.01em; line-height: 1.25; }
	.note-title textarea { display: block; width: 100%; min-width: 0; border: 0; border-radius: var(--st-radius-sm); padding: 0; background: transparent; color: var(--st-fg); font: inherit; font-size: inherit !important; letter-spacing: inherit; line-height: inherit; resize: none; overflow: hidden; overflow-wrap: anywhere; }
	.note-title textarea:hover { background: var(--st-hover); }
	.note-title textarea:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
	.note-title textarea::placeholder { color: var(--st-mfg); }
	.note-more { position: relative; }
	.note-menu { position: absolute; right: 0; top: calc(100% + 4px); z-index: 20; min-width: 160px; padding: 4px; border: 1px solid var(--st-border); border-radius: var(--st-radius-md); background: var(--popover); box-shadow: var(--menu-shadow); }
	.note-menu button { width: 100%; justify-content: flex-start; }
	/* The document starts 24px under the collapsed Tags & files row, framed (Edit) or not (Preview). */
	.note-writing { display: flex; flex-direction: column; gap: 24px; padding-top: 12px; }
	.note-fields { min-width: 0; }
	.note-fields summary { width: fit-content; cursor: pointer; color: var(--st-mfg); font-size: 13px; padding-block: 6px; }
	.note-fields summary:hover { color: var(--st-fg); }
	.note-fields summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.note-fields-content { display: grid; gap: 12px; padding-top: 12px; }
	.note-tags, .note-attachments, .note-attachment { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
	.note-attachment { max-width: 100%; flex-wrap: nowrap; gap: 2px; }
	.note-attachment-link { min-width: 0; text-decoration: none; }
	.note-attachment-link span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.note-tag-input { min-width: 100px; max-width: 100%; width: 160px; height: 32px; padding: 0 10px; border: 1px solid var(--st-input); border-radius: var(--st-radius-md); background: var(--st-field); color: var(--st-fg); font-size: 13px; }
	.note-tag-input::placeholder, .note-textarea::placeholder { color: var(--st-mfg); opacity: 1; }
	.note-content, .note-textarea-wrap { position: relative; min-width: 0; }
	.note-content.drag-over { outline: 2px dashed var(--primary); outline-offset: 2px; }
	.note-textarea { display: block; width: 100%; min-height: 320px; padding: 14px 16px; border: 1px solid var(--st-input); border-radius: var(--st-radius-md); background: var(--st-field); color: var(--st-fg); font: inherit; font-size: 14px; line-height: 1.7; resize: none; overflow: hidden; }
	.note-textarea:focus-visible { outline-offset: 4px; }
	.note-drop { position: absolute; inset: 0; display: grid; place-items: center; background: var(--st-bg); color: var(--st-fg); font-size: 14px; pointer-events: none; }
	.note-preview { max-width: none; min-height: 320px; padding: 0 0 12px; color: var(--st-fg); font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
	.note-preview :global(> :first-child) { margin-top: 0; }
	.note-preview :global(ul), .note-preview :global(ol) { padding-inline-start: 24px; }
	.note-preview :global(li:has(> input[type='checkbox'])),
	.note-preview :global(li:has(> p > input[type='checkbox'])) { position: relative; list-style: none; margin-inline-start: -24px; padding-inline-start: 24px; }
	.note-preview :global(li input[type='checkbox']) { appearance: none; position: absolute; inset-inline-start: 0; top: 0.28em; width: 16px; height: 16px; margin: 0; border: 1.5px solid var(--st-mfg); border-radius: 3px; background: transparent; opacity: 1; pointer-events: none; }
	.note-preview :global(li input[type='checkbox']:checked) { border-color: var(--st-primary); background: var(--st-primary); }
	.note-preview :global(li input[type='checkbox']:checked::after) { content: ''; position: absolute; left: 4px; top: 1px; width: 5px; height: 9px; border: solid var(--st-pfg); border-width: 0 2px 2px 0; transform: rotate(45deg); }
	.note-preview :global(.note-ref) { color: var(--primary); text-decoration: underline; text-underline-offset: 3px; }
	.note-preview :global(.note-ref--missing) { color: var(--st-mfg); text-decoration: none; }
	@media (pointer: coarse), (max-width: 600px) {
		.note-title { font-size: 22px; line-height: 1.3; }
		/* Same place as desktop: Tags & files directly under the tabs, the
		   document 24px below it. The drawn row keeps the desktop height; an
		   invisible ::after lends the summary its 44px tap area. */
		.note-fields summary { position: relative; }
		.note-fields summary::after { content: ''; position: absolute; left: 0; right: 0; top: 50%; height: 44px; transform: translateY(-50%); }
		.note-tag-input { height: 44px; font-size: 16px; }
		.note-textarea { font-size: 16px; overflow-y: auto; }
	}
</style>

