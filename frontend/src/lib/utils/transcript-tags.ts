/** Literal, lazy-first-close parsing, not XML (nested openers are body text). */
export function firstTaggedText(text: string, tag: string): string | undefined {
	const opening = `<${tag}>`;
	const start = text.indexOf(opening);
	if (start === -1) return undefined;
	const body = start + opening.length;
	const end = text.indexOf(`</${tag}>`, body);
	return end === -1 ? undefined : text.slice(body, end);
}

/** The old global block match and replacement, with monotone literal searches. */
export function splitTaggedBlocks(text: string, tag: string): { blocks: string[]; outside: string } {
	const opening = `<${tag}>`;
	const closing = `</${tag}>`;
	const blocks: string[] = [];
	const kept: string[] = [];
	let cursor = 0;
	let start: number;
	while ((start = text.indexOf(opening, cursor)) !== -1) {
		const close = text.indexOf(closing, start + opening.length);
		// No later opener can succeed either; don't rescan an unclosed suffix.
		if (close === -1) break;
		const end = close + closing.length;
		kept.push(text.slice(cursor, start));
		blocks.push(text.slice(start, end));
		cursor = end;
	}
	kept.push(text.slice(cursor));
	return { blocks, outside: kept.join('') };
}
