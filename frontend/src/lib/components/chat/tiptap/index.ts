// Tiptap editor components and utilities for chat input
export { FileChipNode, type FileChipAttributes } from './FileChipNode';
export { createChatEditor, type EditorOptions, type Editor } from './editorConfig';
export {
	serializeToMessage,
	extractFileChips,
	isEditorEmpty,
	getPlainText,
	findActiveAtMention,
	getSlashCommand,
	// Shared @ mention utilities for plain text
	findLastAtMentionIndex,
	hasAtMention,
	extractAtMentionQuery
} from './serialization';
