import type { Editor } from '@tiptap/core';
import type { FileChipAttributes } from './FileChipNode';
import type { JSONContent } from '@tiptap/core';

/**
 * Serialize editor content to plain text with @path references.
 *
 * FileChip nodes are converted to @path format for backend compatibility.
 * Regular text is preserved as-is.
 *
 * Example:
 *   Editor: "Check this file [chip:api.ts] and fix it"
 *   Output: "Check this file @src/lib/api.ts and fix it"
 */
export function serializeToMessage(editor: Editor): string {
	const json = editor.getJSON();
	return serializeNode(json);
}

/**
 * Recursively serialize a node to text
 */
function serializeNode(node: JSONContent): string {
	let result = '';

	if (node.type === 'text') {
		result += node.text || '';
	} else if (node.type === 'fileChip') {
		const attrs = node.attrs as FileChipAttributes;
		result += `@${attrs.path}`;
	} else if (node.type === 'hardBreak') {
		result += '\n';
	} else if (node.type === 'paragraph') {
		// Process paragraph content
		if (node.content) {
			result += node.content.map(serializeNode).join('');
		}
		// Add newline after paragraph (except for last one)
		result += '\n';
	} else if (node.type === 'doc') {
		// Process document content
		if (node.content) {
			const parts = node.content.map(serializeNode);
			// Join and remove trailing newline
			result = parts.join('').replace(/\n$/, '');
		}
	} else if (node.content) {
		// Generic handling for other nodes with content
		result += node.content.map(serializeNode).join('');
	}

	return result;
}

/**
 * Extract all file chips from editor content.
 *
 * Useful for tracking which files are referenced in a message.
 */
export function extractFileChips(editor: Editor): FileChipAttributes[] {
	const chips: FileChipAttributes[] = [];
	const json = editor.getJSON();

	function findChips(node: JSONContent) {
		if (node.type === 'fileChip') {
			chips.push(node.attrs as FileChipAttributes);
		}
		if (node.content) {
			node.content.forEach(findChips);
		}
	}

	findChips(json);
	return chips;
}

/**
 * Check if the editor content is effectively empty.
 *
 * Returns true if the editor contains only whitespace or is truly empty.
 */
export function isEditorEmpty(editor: Editor): boolean {
	const text = editor.getText();
	return !text.trim();
}

/**
 * Get the plain text content of the editor (without @path conversion).
 *
 * FileChip nodes are rendered as their filename only.
 */
export function getPlainText(editor: Editor): string {
	return editor.getText();
}

/**
 * Find an active @ mention at the cursor position.
 *
 * Returns the position and query text if an @ mention is being typed,
 * or null if no active mention.
 *
 * Example:
 *   Text: "check @src/lib/api|" (cursor at |)
 *   Returns: { from: 6, to: 19, query: "src/lib/api" }
 */
export function findActiveAtMention(editor: Editor): { from: number; to: number; query: string } | null {
	const { state } = editor;
	const { from } = state.selection;

	// Walk backwards from cursor to find @ symbol
	// This correctly handles document positions with non-text nodes
	let pos = from;
	let query = '';
	let foundAt = false;
	let atPos = -1;

	// Search backwards character by character
	while (pos > 0) {
		const $pos = state.doc.resolve(pos);
		const before = pos > 0 ? state.doc.textBetween(pos - 1, pos, '\0', '\0') : '';

		if (before === '@') {
			// Found @, check if it's at start or after whitespace
			const charBefore = pos > 1 ? state.doc.textBetween(pos - 2, pos - 1, '\0', '\0') : '';
			if (pos === 1 || charBefore === '' || /\s/.test(charBefore)) {
				foundAt = true;
				atPos = pos - 1;
				break;
			} else {
				// @ is part of text (like email), not a mention
				return null;
			}
		} else if (/\s/.test(before) || before === '\0' || before === '') {
			// Hit whitespace or node boundary without finding @
			return null;
		} else {
			// Valid path character, prepend to query
			query = before + query;
		}

		pos--;
	}

	if (!foundAt) {
		return null;
	}

	return {
		from: atPos,
		to: from,
		query
	};
}

/**
 * Check if the editor content starts with a slash command.
 *
 * Returns the command text (without slash) if present, or null.
 */
export function getSlashCommand(editor: Editor): string | null {
	const text = editor.getText().trim();
	if (text.startsWith('/')) {
		// Extract command (everything after / until first space or end)
		const match = text.match(/^\/(\S*)/);
		return match ? match[1] : '';
	}
	return null;
}
