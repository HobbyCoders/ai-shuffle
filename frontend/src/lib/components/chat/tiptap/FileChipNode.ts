import { Node, mergeAttributes } from '@tiptap/core';
import type { Node as ProseMirrorNode } from '@tiptap/pm/model';

/**
 * Attributes for a FileChip node
 */
export interface FileChipAttributes {
	path: string;
	filename: string;
	fullPath: string;
	size: number;
	type: 'file' | 'chat_history' | 'note';
}

declare module '@tiptap/core' {
	interface Commands<ReturnType> {
		fileChip: {
			/**
			 * Insert a file chip at the current cursor position
			 */
			insertFileChip: (attrs: FileChipAttributes) => ReturnType;
		};
	}
}

/**
 * Custom Tiptap node for inline file chips.
 *
 * This is an atomic inline node that represents a file reference.
 * It cannot be edited directly - it's deleted as a unit.
 */
export const FileChipNode = Node.create({
	name: 'fileChip',
	group: 'inline',
	inline: true,
	atom: true, // Atomic - cursor can't enter, delete as unit
	selectable: true,
	draggable: false,

	addAttributes() {
		return {
			path: {
				default: '',
				parseHTML: (element) => element.getAttribute('data-path'),
				renderHTML: (attributes) => ({ 'data-path': attributes.path })
			},
			filename: {
				default: '',
				parseHTML: (element) => element.getAttribute('data-filename'),
				renderHTML: (attributes) => ({ 'data-filename': attributes.filename })
			},
			fullPath: {
				default: '',
				parseHTML: (element) => element.getAttribute('data-full-path'),
				renderHTML: (attributes) => ({ 'data-full-path': attributes.fullPath })
			},
			size: {
				default: 0,
				parseHTML: (element) => parseInt(element.getAttribute('data-size') || '0', 10),
				renderHTML: (attributes) => ({ 'data-size': String(attributes.size) })
			},
			type: {
				default: 'file',
				parseHTML: (element) => element.getAttribute('data-type') || 'file',
				renderHTML: (attributes) => ({ 'data-type': attributes.type })
			}
		};
	},

	parseHTML() {
		return [
			{
				tag: 'span[data-file-chip]'
			}
		];
	},

	renderHTML({ HTMLAttributes, node }) {
		const attrs = node.attrs as FileChipAttributes;
		const typeClass = attrs.type === 'chat_history'
			? 'file-chip-chat-history'
			: attrs.type === 'note'
				? 'file-chip-note'
				: '';

		// Get appropriate icon based on type
		const icon = getFileIcon(attrs.type);

		return [
			'span',
			mergeAttributes(
				{
					'data-file-chip': '',
					class: `file-chip-node ${typeClass}`.trim(),
					contenteditable: 'false'
				},
				HTMLAttributes
			),
			[
				'span',
				{ class: 'file-chip-icon' },
				icon
			],
			[
				'span',
				{ class: 'file-chip-name' },
				attrs.filename
			],
			[
				'button',
				{
					class: 'file-chip-remove',
					type: 'button',
					'aria-label': 'Remove file'
				},
				'\u00D7' // × symbol
			]
		];
	},

	addCommands() {
		return {
			insertFileChip:
				(attrs: FileChipAttributes) =>
				({ commands }) => {
					return commands.insertContent({
						type: this.name,
						attrs
					});
				}
		};
	},

	addNodeView() {
		return ({ node, getPos, editor }) => {
			const dom = document.createElement('span');
			const attrs = node.attrs as FileChipAttributes;

			// Set classes
			dom.className = 'file-chip-node';
			if (attrs.type === 'chat_history') {
				dom.classList.add('file-chip-chat-history');
			} else if (attrs.type === 'note') {
				dom.classList.add('file-chip-note');
			}

			dom.setAttribute('data-file-chip', '');
			dom.setAttribute('data-path', attrs.path);
			dom.setAttribute('data-type', attrs.type);
			dom.contentEditable = 'false';

			// Icon
			const iconSpan = document.createElement('span');
			iconSpan.className = 'file-chip-icon';
			iconSpan.innerHTML = getFileIcon(attrs.type);
			dom.appendChild(iconSpan);

			// Filename
			const nameSpan = document.createElement('span');
			nameSpan.className = 'file-chip-name';
			nameSpan.textContent = attrs.filename;
			dom.appendChild(nameSpan);

			// Remove button
			const removeBtn = document.createElement('button');
			removeBtn.className = 'file-chip-remove';
			removeBtn.type = 'button';
			removeBtn.setAttribute('aria-label', 'Remove file');
			removeBtn.innerHTML = '&times;';
			removeBtn.addEventListener('click', (e) => {
				e.preventDefault();
				e.stopPropagation();
				const pos = getPos();
				if (typeof pos === 'number') {
					const chipEnd = pos + node.nodeSize;
					// Check if there's a trailing space after the chip and delete it too
					const docSize = editor.state.doc.content.size;
					let deleteEnd = chipEnd;
					if (chipEnd < docSize) {
						const textAfter = editor.state.doc.textBetween(chipEnd, Math.min(chipEnd + 1, docSize));
						if (textAfter === ' ') {
							deleteEnd = chipEnd + 1;
						}
					}
					editor.chain().focus().deleteRange({ from: pos, to: deleteEnd }).run();
				}
			});
			dom.appendChild(removeBtn);

			return {
				dom,
				update: (updatedNode: ProseMirrorNode) => {
					if (updatedNode.type.name !== 'fileChip') {
						return false;
					}
					const newAttrs = updatedNode.attrs as FileChipAttributes;
					nameSpan.textContent = newAttrs.filename;
					iconSpan.innerHTML = getFileIcon(newAttrs.type);
					return true;
				},
				destroy: () => {
					// Cleanup if needed
				}
			};
		};
	}
});

/**
 * Get the appropriate SVG icon for a file type
 */
function getFileIcon(type: string): string {
	switch (type) {
		case 'chat_history':
			// Message bubble icon
			return `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
			</svg>`;
		case 'note':
			// Note/document icon
			return `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"></path>
				<polyline points="14,2 14,8 20,8"></polyline>
			</svg>`;
		default:
			// File icon
			return `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
				<polyline points="13,2 13,9 20,9"></polyline>
			</svg>`;
	}
}

export default FileChipNode;
