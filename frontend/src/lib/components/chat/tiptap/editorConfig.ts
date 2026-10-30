import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import type { EditorView } from '@tiptap/pm/view';
import { FileChipNode } from './FileChipNode';
import { isLargePaste } from './large-paste';

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

/**
 * Insert text as plain text in one transaction and keep the caret in view.
 * Line breaks stay `\n` in the paragraph (the editor renders them pre-wrap).
 */
export function insertPlainText(view: EditorView, text: string): void {
	view.dispatch(view.state.tr.insertText(text.replace(/\r\n?/g, '\n')).scrollIntoView());
}

/** The text of a non-composing `beforeinput` that inserts more than one line, else null. */
export function multiLineTextInput(event: InputEvent): string | null {
	if (event.isComposing) return null;
	if (event.inputType !== 'insertText' && event.inputType !== 'insertReplacementText') return null;
	const text = event.data ?? event.dataTransfer?.getData('text/plain') ?? '';
	return /[\r\n]/.test(text) ? text : null;
}

export interface EditorOptions {
	element: HTMLElement;
	placeholder?: string;
	initialContent?: string;
	onUpdate?: () => void;
	onTransaction?: () => void;
	onFocus?: () => void;
	onBlur?: () => void;
	/**
	 * Offered a paste at or above LARGE_PASTE_BYTES. Return true when it was
	 * taken (as an attachment); false pastes it inline as usual.
	 */
	onLargePaste?: (text: string) => boolean;
	/** True when this paste was Ctrl/Cmd+Shift+V, which always stays inline. */
	consumePlainPaste?: () => boolean;
}

/**
 * Create a configured Tiptap editor for chat input.
 *
 * This editor is configured for plain text with inline file chips.
 * Rich formatting (bold, italic, etc.) is intentionally disabled.
 */
export function createChatEditor(options: EditorOptions): Editor {
	const {
		element, placeholder = 'Message...', initialContent = '', onUpdate, onTransaction, onFocus, onBlur,
		onLargePaste, consumePlainPaste
	} = options;

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
				'data-placeholder': placeholder,
				// The contenteditable is the composer's text field; without these a
				// screen reader meets an unnamed editable region (the placeholder is
				// CSS-only and not announced).
				role: 'textbox',
				'aria-multiline': 'true',
				'aria-label': 'Message'
			},
			// Handle paste to strip formatting
			handlePaste: (view, event) => {
				const text = event.clipboardData?.getData('text/plain');
				if (!text) return false;
				const plain = consumePlainPaste?.() ?? false;
				if (!plain && onLargePaste && isLargePaste(text) && onLargePaste(text)) return true;
				insertPlainText(view, text);
				return true;
			},
			handleDOMEvents: {
				// Multi-line text that arrives as typed input rather than a paste
				// event: an IME commit (Gboard's clipboard chip), dictation or an
				// automation fill. Chrome's native editing would apply it as one
				// paragraph split per line, each re-laying out the growing editor
				// and firing its own `input` (about 1,000 for a 1 KB fill of blank
				// lines: an 11.7 s freeze at 4x CPU, #912). Take it as one plain-text
				// transaction instead, under the same large-text rule as a paste.
				beforeinput: (view, event) => {
					const text = multiLineTextInput(event);
					if (text === null || view.composing || !event.cancelable) return false;
					event.preventDefault();
					if (!(onLargePaste && isLargePaste(text) && onLargePaste(text))) insertPlainText(view, text);
					return true;
				}
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
