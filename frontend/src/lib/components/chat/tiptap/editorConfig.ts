import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import { FileChipNode } from './FileChipNode';

/**
 * Parse initial content string: supports Tiptap JSON (preserves file chips)
 * or plain text (wrapped in a doc/paragraph structure).
 */
function parseInitialContent(content: string): string | Record<string, unknown> {
	if (!content) return '';
	if (content.startsWith('{')) {
		try {
			const parsed = JSON.parse(content);
			if (parsed && parsed.type === 'doc') return parsed;
		} catch { /* not JSON, fall through to plain text */ }
	}
	return {
		type: 'doc',
		content: [
			{
				type: 'paragraph',
				content: [{ type: 'text', text: content }]
			}
		]
	};
}

/**
 * Live placeholder text per editor instance. The Placeholder extension reads its
 * text through a callback, so updating the box makes the next decoration pass
 * pick up the new text without recreating the editor.
 */
const editorPlaceholders = new WeakMap<Editor, { text: string }>();

/**
 * Update the placeholder text of a live editor and force decorations to recompute.
 */
export function setEditorPlaceholder(editor: Editor, text: string): void {
	const box = editorPlaceholders.get(editor);
	if (!box || box.text === text) return;
	box.text = text;
	editor.view.dispatch(editor.state.tr);
}

export interface EditorOptions {
	element: HTMLElement;
	placeholder?: string;
	initialContent?: string;
	onUpdate?: () => void;
	onTransaction?: () => void;
	onFocus?: () => void;
	onBlur?: () => void;
}

/**
 * Create a configured Tiptap editor for chat input.
 *
 * This editor is configured for plain text with inline file chips.
 * Rich formatting (bold, italic, etc.) is intentionally disabled.
 */
export function createChatEditor(options: EditorOptions): Editor {
	const { element, placeholder = 'Message...', initialContent = '', onUpdate, onTransaction, onFocus, onBlur } = options;

	// Captured by the placeholder callback so the first decoration pass (which runs
	// inside `new Editor`, before the instance exists to key the WeakMap on) is correct.
	const placeholderBox = { text: placeholder };

	const editor = new Editor({
		element,
		extensions: [
			StarterKit.configure({
				// Disable rich formatting features we don't need
				heading: false,
				bulletList: false,
				orderedList: false,
				blockquote: false,
				codeBlock: false,
				horizontalRule: false,
				bold: false,
				italic: false,
				strike: false,
				code: false,
				listItem: false,
				dropcursor: false,
				gapcursor: false
				// document, paragraph, text, history, hardBreak are enabled by default
			}),
			Placeholder.configure({
				placeholder: () => placeholderBox.text,
				emptyEditorClass: 'is-editor-empty'
			}),
			FileChipNode
		],
		content: parseInitialContent(initialContent),
		editorProps: {
			attributes: {
				class: 'chat-editor-content',
				'data-placeholder': placeholder
			},
			// Handle paste to strip formatting
			handlePaste: (view, event) => {
				const text = event.clipboardData?.getData('text/plain');
				if (text) {
					// Insert as plain text
					view.dispatch(view.state.tr.insertText(text));
					return true;
				}
				return false;
			}
		},
		onUpdate: () => {
			onUpdate?.();
		},
		onTransaction: () => {
			onTransaction?.();
		},
		onFocus: () => {
			onFocus?.();
		},
		onBlur: () => {
			onBlur?.();
		}
	});

	editorPlaceholders.set(editor, placeholderBox);
	return editor;
}

export type { Editor };
