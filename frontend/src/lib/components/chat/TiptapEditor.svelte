<script lang="ts">
	/**
	 * TiptapEditor - Rich text editor wrapper for chat input
	 *
	 * Provides a Tiptap-based editor with:
	 * - Plain text editing with emoji support
	 * - Inline file chip nodes
	 * - Keyboard shortcuts for sending messages
	 */
	import { onMount, onDestroy } from 'svelte';
	import { createChatEditor, type Editor } from './tiptap/editorConfig';
	import type { FileChipAttributes } from './tiptap/FileChipNode';
	import { serializeToMessage, isEditorEmpty, findActiveAtMention, getSlashCommand } from './tiptap/serialization';

	interface Props {
		placeholder?: string;
		initialContent?: string;
		disabled?: boolean;
		onTextChange?: (text: string) => void;
		onKeyDown?: (event: KeyboardEvent) => boolean | void;
	}

	let {
		placeholder = 'Message...',
		initialContent = '',
		disabled = false,
		onTextChange,
		onKeyDown
	}: Props = $props();

	let editorElement = $state<HTMLDivElement | null>(null);
	let editor = $state<Editor | null>(null);
	let isFocused = $state(false);

	onMount(() => {
		if (editorElement) {
			editor = createChatEditor({
				element: editorElement,
				placeholder,
				initialContent,
				onUpdate: () => {
					// Use getMessage() to include chip @path references in the text
					// This ensures send button enables when chips are present
					onTextChange?.(serializeToMessage(editor!));
				},
				onFocus: () => {
					isFocused = true;
				},
				onBlur: () => {
					isFocused = false;
				}
			});

			// Add keydown handler to the editor element
			editorElement.addEventListener('keydown', handleEditorKeyDown);
		}
	});

	onDestroy(() => {
		if (editorElement) {
			editorElement.removeEventListener('keydown', handleEditorKeyDown);
		}
		editor?.destroy();
	});

	function handleEditorKeyDown(event: KeyboardEvent) {
		// Let parent component handle first (for autocomplete, etc.)
		if (onKeyDown) {
			const handled = onKeyDown(event);
			if (handled) return;
		}
	}

	// ==================== Public API ====================

	/**
	 * Get the underlying Tiptap editor instance
	 */
	export function getEditor(): Editor | null {
		return editor;
	}

	/**
	 * Focus the editor
	 */
	export function focus() {
		editor?.commands.focus();
	}

	/**
	 * Clear the editor content
	 */
	export function clear() {
		editor?.commands.clearContent();
	}

	/**
	 * Set the editor content (plain text)
	 */
	export function setContent(content: string) {
		if (!editor) return;
		editor.commands.setContent(
			content
				? {
						type: 'doc',
						content: [
							{
								type: 'paragraph',
								content: [{ type: 'text', text: content }]
							}
						]
					}
				: ''
		);
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
</script>

<div
	bind:this={editorElement}
	class="tiptap-editor"
	class:focused={isFocused}
	class:disabled
></div>

<style>
	.tiptap-editor {
		width: 100%;
		min-height: 24px;
		max-height: 300px;
		overflow-y: auto;
	}

	.tiptap-editor.disabled {
		opacity: 0.5;
		pointer-events: none;
	}

	/* ProseMirror editor styles - match original textarea */
	.tiptap-editor :global(.ProseMirror) {
		outline: none;
		width: 100%;
		min-height: 24px;
		padding: 0;
		margin: 0;
		background: transparent;
		border: none;
		font-family: 'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
		font-size: 15px;
		font-weight: 400;
		line-height: 1.5;
		color: var(--foreground);
		/* Align text baseline with send button center (32px button, so ~6px offset) */
		padding-top: 4px;
	}

	.tiptap-editor :global(.ProseMirror p) {
		margin: 0;
	}

	/* Placeholder styling */
	.tiptap-editor :global(.ProseMirror p.is-editor-empty:first-child::before) {
		content: attr(data-placeholder);
		color: var(--input-placeholder);
		font-weight: 300;
		pointer-events: none;
		float: left;
		height: 0;
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
		opacity: 0;
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
