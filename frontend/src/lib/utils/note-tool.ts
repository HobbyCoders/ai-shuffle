/**
 * Fluid Note tool presentation.
 *
 * The Note tool is a scratchpad: every mutation recites the full current list,
 * and notes have no per-item status. The chat UI must not invent "completed"
 * vs "in progress" from list position — that is how it used to look like a
 * todo bar where earlier thoughts appeared done just because a new one landed.
 */

export interface NoteToolMessage {
	toolResult?: unknown;
	toolInput?: Record<string, unknown> | null;
	partialToolInput?: string;
}

/** Split an add payload into individual notes (one per line, stripping "- "). */
export function splitNoteText(text: string): string[] {
	const lines: string[] = [];
	for (const raw of text.split('\n')) {
		let line = raw.trim();
		if (line.startsWith('- ')) line = line.slice(2).trim();
		if (line) lines.push(line);
	}
	return lines;
}

function dashLinesFrom(text: string): string[] {
	const lines: string[] = [];
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (line.startsWith('- ')) {
			const body = line.slice(2).trim();
			if (body) lines.push(body);
		}
	}
	return lines;
}

function notesFromInput(
	input: Record<string, unknown> | null | undefined,
	partial: string | undefined
): string[] {
	const lines: string[] = [];
	const text =
		typeof input?.text === 'string'
			? input.text
			: extractPartialString(partial, 'text');
	if (text) lines.push(...splitNoteText(text));

	const texts = input?.texts;
	if (Array.isArray(texts)) {
		for (const item of texts) {
			if (typeof item === 'string') lines.push(...splitNoteText(item));
		}
	}
	return lines;
}

/** Best-effort `"field": "value"` pull from a still-streaming JSON buffer. */
function extractPartialString(partial: string | undefined, field: string): string {
	if (!partial) return '';
	const match = partial.match(new RegExp(`"${field}"\\s*:\\s*"`));
	if (!match || match.index === undefined) return '';
	let out = '';
	let i = match.index + match[0].length;
	while (i < partial.length) {
		const char = partial[i];
		if (char === '\\' && i + 1 < partial.length) {
			const next = partial[i + 1];
			if (next === 'n') out += '\n';
			else if (next === 'r') out += '\r';
			else if (next === 't') out += '\t';
			else out += next;
			i += 2;
		} else if (char === '"') {
			break;
		} else {
			out += char;
			i += 1;
		}
	}
	return out;
}

/**
 * Current notes to render for a Note tool_use message.
 *
 * Prefers the recited result (the full scratchpad). While the call is still
 * streaming, falls back to the note(s) being written. A cleared/empty list
 * returns null so the row can hide.
 */
export function notesFromToolMessage(message: NoteToolMessage): string[] | null {
	const result = typeof message.toolResult === 'string' ? message.toolResult : '';
	if (result) {
		if (result.includes('(empty)')) return null;
		const lines = dashLinesFrom(result);
		return lines.length > 0 ? lines : null;
	}
	const pending = notesFromInput(message.toolInput, message.partialToolInput);
	return pending.length > 0 ? pending : null;
}
