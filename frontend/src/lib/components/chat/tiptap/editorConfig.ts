import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import { FileChipNode } from './FileChipNode';

export interface EditorOptions {
	element: HTMLElement;
	placeholder?: string;
	initialContent?: string;
	onUpdate?: (text: string) => void;
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

	return new Editor({
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
				placeholder,
				emptyEditorClass: 'is-editor-empty'
			}),
			FileChipNode
		],
		content: initialContent
			? {
					type: 'doc',
					content: [
						{
							type: 'paragraph',
							content: [{ type: 'text', text: initialContent }]
						}
					]
				}
			: '',
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
		onUpdate: ({ editor }) => {
			onUpdate?.(editor.getText());
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
}

export type { Editor };
