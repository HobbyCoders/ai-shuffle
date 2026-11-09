import type { ChatMessage } from '$lib/stores/types';
import { agentDelivery } from './agent-delivery';

/** A steer has no independent checkpoint. Legacy normal prompts retain content resolution. */
export function canRewindUserMessage(message: ChatMessage, earlierPromptInTurn = false): boolean {
	if (message.role !== 'user' || message.type === 'command' || agentDelivery(message)) return false;
	if (message.metadata?.not_sent) return true; // row-only restore, not a model checkpoint
	if (message.checkpointBacked === false) return false;
	if (message.checkpointBacked === true || message.jsonlUuid) return true;
	return !earlierPromptInTurn;
}

/** A stretch of a user message: its words, or an upload the send recorded. */
export type UploadMentionPart = string | { path: string };

/** Cut `@path` mentions of the recorded uploads out of `text`, keeping their place. */
export function splitUploadMentions(text: string, uploadPaths: string[]): UploadMentionPart[] {
	const paths = [...new Set(uploadPaths.filter(Boolean))].sort((a, b) => b.length - a.length);
	const parts: UploadMentionPart[] = [];
	let offset = 0;
	while (offset < text.length) {
		let at = -1;
		let path = '';
		for (const candidate of paths) {
			const index = text.indexOf('@' + candidate, offset);
			if (index !== -1 && (at === -1 || index < at)) { at = index; path = candidate; }
		}
		if (at === -1) { parts.push(text.slice(offset)); break; }
		if (at > offset) parts.push(text.slice(offset, at));
		parts.push({ path });
		offset = at + path.length + 1;
	}
	return parts;
}

/** What a sent bubble shows: words with chips in place, then any upload never mentioned. */
export function userBubbleParts(content: string, uploadPaths: string[]): UploadMentionPart[] {
	if (!uploadPaths.length) return [content];
	const parts = splitUploadMentions(content, uploadPaths);
	const shown = new Set(parts.flatMap(part => typeof part === 'string' ? [] : [part.path]));
	return [...parts, ...[...new Set(uploadPaths.filter(Boolean))].filter(path => !shown.has(path)).map(path => ({ path }))];
}

export const uploadFileName = (path: string): string => path.split(/[/\\]/).pop() || 'attachment';

export const MAX_COLLAPSED_USER_MESSAGE_LINES = 8;
export const MAX_COLLAPSED_USER_MESSAGE_LENGTH = 600;

/** T3-style guard: only genuinely long prompts get disclosure chrome. */
export function shouldCollapseUserMessage(content: string): boolean {
	if (!content.trim()) return false;
	return (
		content.length > MAX_COLLAPSED_USER_MESSAGE_LENGTH ||
		content.split('\n').length > MAX_COLLAPSED_USER_MESSAGE_LINES
	);
}
