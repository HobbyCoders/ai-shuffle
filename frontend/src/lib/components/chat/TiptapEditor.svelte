<script lang="ts">
	/**
	 * TiptapEditor - Rich text editor wrapper for chat input
	 *
	 * Provides a Tiptap-based editor with:
	 * - Plain text editing with emoji support
	 * - Inline file chip nodes
	 * - Keyboard shortcuts for sending messages
	 */
	import { onMount, onDestroy, tick } from 'svelte';
	import { createChatEditor, setEditorPlaceholder, type Editor } from './tiptap/editorConfig';
	import { createLiveDictation } from './tiptap/liveDictation';
	import type { FileChipAttributes } from './tiptap/FileChipNode';
	import { serializeToMessage, isEditorEmpty, findActiveAtMention, getSlashCommand } from './tiptap/serialization';

	interface Props {
		placeholder?: string;
		suggestion?: string;
		onAcceptSuggestion?: () => void;
		initialContent?: string;
		disabled?: boolean;
		onTextChange?: (text: string) => void;
		onUserInput?: () => void;
		onKeyDown?: (event: KeyboardEvent) => boolean | void;
		onFocusChange?: (focused: boolean) => void;
		onBeforeDestroy?: (editor: Editor) => void;
	}

	let {
		placeholder = 'Message...',
		suggestion = '',
		onAcceptSuggestion,
		initialContent = '',
		disabled = false,
		onTextChange,
		onUserInput,
		onKeyDown,
		onFocusChange,
		onBeforeDestroy
	}: Props = $props();

	let editorElement = $state<HTMLDivElement | null>(null);
	let editor = $state<Editor | null>(null);
	let isFocused = $state(false);
	let editorEmpty = $state(false);
	let liveDictation: ReturnType<typeof createLiveDictation> | null = null;

	// Store reference for cleanup to handle edge cases where editorElement might change
	let boundElement: HTMLDivElement | null = null;

	onMount(() => {
		if (editorElement) {
			boundElement = editorElement;
			editor = createChatEditor({
				element: editorElement,
				placeholder,
				initialContent,
				onUpdate: () => {
					// Use getMessage() to include chip @path references in the text
					// This ensures send button enables when chips are present
					if (editor) {
						editorEmpty = editor.isEmpty;
						onTextChange?.(serializeToMessage(editor));
					}
				},
				onFocus: () => {
					isFocused = true;
					onFocusChange?.(true);
				},
				onBlur: () => {
					// Defer state mutation to avoid Svelte 5 state_unsafe_mutation error
					// The blur event can fire during render phase, so we use tick() to
					// ensure the mutation happens after the current render completes
					tick().then(() => {
						isFocused = false;
						onFocusChange?.(false);
					});
				}
			});
			editorEmpty = editor.isEmpty;

			liveDictation = createLiveDictation(editor);

			// Add keydown handler to the editor element
			boundElement.addEventListener('dblclick', handleSuggestionDoubleClick);
			boundElement.addEventListener('pointerup', handleSuggestionTap);
			boundElement.addEventListener('keydown', handleEditorKeyDown, true);
			boundElement.addEventListener('beforeinput', handleUserInput);
			boundElement.addEventListener('input', handleUserInput);
			boundElement.addEventListener('compositionstart', handleUserInput);
			boundElement.addEventListener('paste', handleUserInput);
			boundElement.addEventListener('drop', handleUserInput);
		}
	});

	onDestroy(() => {
		// Parent refs may already be cleared. Hand off capture before destruction.
		if (editor && !editor.isDestroyed) onBeforeDestroy?.(editor);
		if (boundElement) {
			boundElement.removeEventListener('dblclick', handleSuggestionDoubleClick);
			boundElement.removeEventListener('pointerup', handleSuggestionTap);
			boundElement.removeEventListener('keydown', handleEditorKeyDown, true);
			boundElement.removeEventListener('beforeinput', handleUserInput);
			boundElement.removeEventListener('input', handleUserInput);
			boundElement.removeEventListener('compositionstart', handleUserInput);
			boundElement.removeEventListener('paste', handleUserInput);
			boundElement.removeEventListener('drop', handleUserInput);
		}
		liveDictation?.destroy();
		liveDictation = null;
		editor?.destroy();
		editor = null;
	});

	// Update TipTap's placeholder when the prop changes (e.g. when profile changes)
	$effect(() => {
		const newPlaceholder = suggestion && onAcceptSuggestion && !disabled ? '' : suggestion || placeholder;
		if (!editor) return;
		setEditorPlaceholder(editor, newPlaceholder);
	});

	function handleUserInput() { onUserInput?.(); }

	function handleEditorKeyDown(event: KeyboardEvent) {
		// Let parent component handle first (for autocomplete, etc.)
		if (onKeyDown) {
			const handled = onKeyDown(event);
			if (handled) return;
		}
	}

	// A single pointer interaction only focuses the real editor. Touch devices
	// do not consistently emit dblclick, so recognize two nearby touch releases.
	let lastSuggestionTap: { time: number; x: number; y: number; suggestion: string } | null = null;
	function canAcceptSuggestion() {
		return !!suggestion && !!onAcceptSuggestion && !disabled && !!editor?.isEmpty;
	}
	function handleSuggestionDoubleClick(event: MouseEvent) {
		if (!canAcceptSuggestion()) return;
		event.preventDefault();
		lastSuggestionTap = null;
		onAcceptSuggestion?.();
	}
	function handleSuggestionTap(event: PointerEvent) {
		if (event.pointerType !== 'touch' || !event.isPrimary || !canAcceptSuggestion()) {
			lastSuggestionTap = null;
			return;
		}
		const previous = lastSuggestionTap;
		lastSuggestionTap = { time: event.timeStamp, x: event.clientX, y: event.clientY, suggestion };
		if (previous && previous.suggestion === suggestion && event.timeStamp - previous.time <= 350 &&
			Math.hypot(event.clientX - previous.x, event.clientY - previous.y) <= 24) {
			event.preventDefault();
			lastSuggestionTap = null;
			onAcceptSuggestion?.();
		}
	}

	// ==================== Helpers ====================

	/** Try to parse a string as Tiptap JSON doc. Returns the parsed object or null. */
	function tryParseTiptapJSON(str: string): Record<string, unknown> | null {
		if (!str.startsWith('{')) return null;
		try {
			const parsed = JSON.parse(str);
			if (parsed && parsed.type === 'doc') return parsed;
		} catch { /* not JSON */ }
		return null;
	}

	// ==================== Public API ====================

	/**
	 * Get the underlying Tiptap editor instance
	 */
	export function getEditor(): Editor | null {
		return editor;
	}

	/**
	 * Get the editor content as Tiptap JSON (preserves file chips).
	 */
	export function getJSON(): Record<string, unknown> | null {
		return editor?.getJSON() as Record<string, unknown> | null ?? null;
	}

	/**
	 * Focus the editor
	 */
	export function focus() {
		editor?.commands.focus();
	}

	/** Blur the editor, allowing the mobile composer to return to its compact state. */
	export function blur() {
		editor?.commands.blur();
	}

	/** Model suggestions are text, never HTML or an editor JSON document. */
	export function setPlainText(text: string) {
		editor?.commands.setContent({
			type: 'doc',
			content: text.split('\n').map(line => ({
				type: 'paragraph',
				...(line ? { content: [{ type: 'text', text: line }] } : {})
			}))
		});
	}

	/**
	 * Clear the editor content
	 */
	export function clear() {
		editor?.commands.clearContent();
	}

	/**
	 * Set the editor content.
	 * Accepts either plain text or a JSON string (Tiptap doc structure).
	 * JSON strings preserve file chips across session switches.
	 */
	export function setContent(content: string) {
		liveDictation?.commit();
		if (!editor) return;
		if (!content) {
			editor.commands.setContent('');
			return;
		}
		// Try parsing as Tiptap JSON (preserves file chip nodes)
		const json = tryParseTiptapJSON(content);
		if (json) {
			editor.commands.setContent(json);
		} else {
			editor.commands.setContent({
				type: 'doc',
				content: [
					{
						type: 'paragraph',
						content: [{ type: 'text', text: content }]
					}
				]
			});
		}
	}

	/**
	 * Insert a file chip at the current cursor position
	 */
	export function insertFileChip(attrs: FileChipAttributes) {
		if (!editor) return;
		editor.chain().focus().insertFileChip(attrs).insertContent(' ').run();
	}

	/**
	 * Insert a file chip, replacing an active @ mention if present
	 */
	export function insertFileChipReplacingMention(attrs: FileChipAttributes) {
		if (!editor) return;

		const mention = findActiveAtMention(editor);
		if (mention) {
			// Delete the @query and insert chip
			editor
				.chain()
				.focus()
				.deleteRange({ from: mention.from, to: mention.to })
				.insertFileChip(attrs)
				.insertContent(' ')
				.run();
		} else {
			// No active mention, just insert at cursor
			insertFileChip(attrs);
		}
	}

	/**
	 * Replace the @ mention query text (for directory navigation)
	 */
	export function replaceAtMention(newPath: string) {
		if (!editor) return;

		const mention = findActiveAtMention(editor);
		if (mention) {
			editor
				.chain()
				.focus()
				.deleteRange({ from: mention.from, to: mention.to })
				.insertContent(`@${newPath}`)
				.run();
		}
	}

	/**
	 * Get the serialized message content (text with @path references)
	 */
	export function getMessage(): string {
		if (!editor) return '';
		return serializeToMessage(editor);
	}

	/**
	 * Check if the editor is effectively empty
	 */
	export function isEmpty(): boolean {
		if (!editor) return true;
		return isEditorEmpty(editor);
	}

	/**
	 * Get plain text content (without @path conversion)
	 */
	export function getText(): string {
		return editor?.getText() ?? '';
	}

	/**
	 * Check if content starts with a slash command
	 */
	export function getSlashCmd(): string | null {
		if (!editor) return null;
		return getSlashCommand(editor);
	}

	/**
	 * Check if there's an active @ mention being typed
	 */
	export function getActiveMention(): { from: number; to: number; query: string } | null {
		if (!editor) return null;
		return findActiveAtMention(editor);
	}

	/**
	 * Append text to the editor (e.g., for voice transcription)
	 */
	export function appendText(text: string) {
		if (!editor) return;
		editor.chain().focus().insertContent(text).run();
	}

	/** Apply the running transcript while preserving manual edits and selection. */
	export function streamText(text: string) {
		liveDictation?.write(text);
	}

	/** Release transcript ownership before starting another dictation. */
	export function commitDictation() {
		liveDictation?.commit();
	}

	export function discardDictation() {
		liveDictation?.discard();
	}
</script>

<div class="editor-shell">
	<div
		bind:this={editorElement}
		class="tiptap-editor"
		class:has-suggestion={!!suggestion}
		data-testid={suggestion ? 'composer-suggestion' : undefined}
		class:focused={isFocused}
		class:disabled
	></div>
	{#if suggestion && onAcceptSuggestion && !disabled && editorEmpty}
		<span class="inline-suggestion" aria-hidden="true">
			{suggestion}
		</span>
	{/if}
</div>

<style>
	.editor-shell { position: relative; width: 100%; min-width: 0; }
	.inline-suggestion {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		inset-inline-start: 0;
		max-width: 100%;
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--subtle-foreground);
		font: inherit;
		font-size: 14.5px;
		line-height: 1.55;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		text-align: start;
		pointer-events: none;
	}
	.tiptap-editor {
		width: 100%;
		min-height: 24px;
		max-height: 300px;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		/* Center the caret and placeholder in the empty min-height box; `safe`
		   falls back to start once the content is taller than the box so the
		   scrollbar still works. */
		justify-content: safe center;
	}

	.tiptap-editor.disabled {
		opacity: 0.5;
		pointer-events: none;
	}

	/* ProseMirror editor styles */
	/* Composer body type (mockup `.inputarea`): 14.5px / 1.55. */
	.tiptap-editor :global(.ProseMirror) {
		position: relative;
		outline: none;
		width: 100%;
		min-height: 24px;
		font-family: var(--font-sans);
		font-size: 14.5px;
		font-weight: 400;
		line-height: 1.55;
		color: var(--foreground);
	}

	.tiptap-editor :global(.ProseMirror p) {
		margin: 0;
	}

	/* Placeholder sits on the text line so it shares the caret's vertical
	   position. height: 0 keeps it out of layout, matching TipTap's default. */
	.tiptap-editor :global(.ProseMirror p.is-editor-empty:first-child::before) {
		content: attr(data-placeholder);
		color: var(--subtle-foreground);
		font-weight: 400;
		pointer-events: none;
		float: left;
		height: 0;
	}

	/* Use the real placeholder line without adding content or height to the draft. */
	.tiptap-editor.has-suggestion :global(.ProseMirror p.is-editor-empty:first-child::before) {
		position: absolute;
		inset-inline: 0;
		float: none;
		height: auto;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* File chip node styling */
	.tiptap-editor :global(.file-chip-node) {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.125rem 0.5rem;
		margin: 0 0.125rem;
		background: var(--chip-primary-bg);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.75rem;
		vertical-align: middle;
		cursor: default;
		user-select: none;
		line-height: 1.4;
	}

	.tiptap-editor :global(.file-chip-node.ProseMirror-selectednode) {
		outline: 2px solid var(--primary);
		outline-offset: 1px;
	}

	/* Type-specific chip colors */
	.tiptap-editor :global(.file-chip-node.file-chip-chat-history) {
		background: color-mix(in oklch, var(--feature-agents) 15%, transparent);
	}

	.tiptap-editor :global(.file-chip-node.file-chip-note) {
		background: var(--status-success-bg);
	}

	.tiptap-editor :global(.file-chip-node.file-chip-subagent) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* Chip icon */
	.tiptap-editor :global(.file-chip-icon) {
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		opacity: 0.7;
	}

	.tiptap-editor :global(.file-chip-icon svg) {
		width: 12px;
		height: 12px;
	}

	/* Image thumbnail inside a chip — larger than the icon slot and flush to the
	   left edge so the preview reads clearly without dwarfing the filename. */
	.tiptap-editor :global(.file-chip-node.file-chip-image) {
		padding-left: 0.125rem;
	}

	.tiptap-editor :global(.file-chip-image .file-chip-icon) {
		opacity: 1;
	}

	.tiptap-editor :global(.file-chip-thumb) {
		width: 20px;
		height: 20px;
		object-fit: cover;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		display: block;
		background: var(--muted);
	}

	/* Chip filename */
	.tiptap-editor :global(.file-chip-name) {
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Chip remove button */
	.tiptap-editor :global(.file-chip-remove) {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 14px;
		padding: 0;
		margin-left: 0.125rem;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0.5;
		transition: opacity 0.15s ease, background-color 0.15s ease;
		font-size: 14px;
		line-height: 1;
	}

	.tiptap-editor :global(.file-chip-node:hover .file-chip-remove) {
		opacity: 0.7;
	}

	.tiptap-editor :global(.file-chip-remove:hover) {
		opacity: 1 !important;
		background: var(--muted);
	}

</style>
