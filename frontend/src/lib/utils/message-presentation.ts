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

// Python's str.strip() whitespace (str.isspace), so a title trims exactly as
// the server's does; JS trim() differs (\ufeff in, \x1c-\x1f and \x85 out).
const PY_SPACE = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');
const pyStrip = (text: string): string => text.replace(PY_STRIP, '');

/** `_cut_known_references`: the prompt with its attachments' `@path`
 *  mentions cut out, the spaces they leave folded; the prompt itself when it
 *  mentions none, '' when it is only attachments. */
function cutKnownReferences(prompt: string, uploadPaths: readonly string[]): string {
	const parts = splitUploadMentions(prompt, [...uploadPaths]);
	if (!parts.some(part => typeof part !== 'string')) return prompt;
	return pyStrip(parts
		.map(part => typeof part === 'string' ? part : ' ')
		.join('')
		.replace(/[ \t]{2,}/g, ' ')
		.replace(/[ \t]*\n[ \t]*/g, '\n'));
}

/** `_attachment_names`: the file names of the attachments a prompt mentions,
 *  in order, once each. */
function attachmentNames(prompt: string, uploadPaths: readonly string[]): string {
	const names = splitUploadMentions(prompt, [...uploadPaths])
		.flatMap(part => typeof part === 'string' ? [] : [uploadFileName(part.path)]);
	return [...new Set(names)].join(', ');
}

/**
 * The typed words of a prompt: its attachments' `@path` mentions cut out
 * wherever they sit (#1654). An automatic branch name is named from these;
 * empty when the prompt is only attachments.
 */
export function typedWords(prompt: string, uploadPaths: readonly string[] | undefined): string {
	return pyStrip(cutKnownReferences(prompt, uploadPaths ?? []));
}

/**
 * A first message's chat title. A port of the server's `derive_session_title`
 * (`app/core/session_title.py`, the definition) for a caller that knows the
 * message's attachments, which the composer always does: the attachments'
 * references cut out (their file names when that leaves nothing), then cut to
 * `maxChars` code points at the last space, with '...'. Both suites assert the
 * same final strings from `attachment-title-cases.json`.
 */
export function deriveSessionTitle(prompt: string, uploadPaths: readonly string[] | undefined, maxChars = 100): string {
	const known = uploadPaths ?? [];
	const body = [...(cutKnownReferences(prompt, known) || attachmentNames(prompt, known))];
	let title = pyStrip(body.slice(0, maxChars).join(''));
	if (body.length > maxChars) {
		const space = title.lastIndexOf(' ');
		title = (space === -1 ? title : title.slice(0, space)) + '...';
	}
	return title;
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
