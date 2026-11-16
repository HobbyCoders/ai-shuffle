/**
 * Length limits in code points, the unit the server's `max_length` counts
 * (JS `.length` and an input's native `maxlength` count UTF-16 units, so an
 * emoji is two of those). Cuts never split a character: an emoji, a ZWJ
 * sequence or a letter with its combining marks stays whole or goes whole.
 */

/** Code points in `text`. */
export function codePoints(text: string): number {
	return Array.from(text).length;
}

function characters(text: string): string[] {
	return typeof Intl !== 'undefined' && 'Segmenter' in Intl
		? Array.from(new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text), (s) => s.segment)
		: Array.from(text);
}

/** `text` cut to at most `max` code points from its start, never inside a character. */
export function cutCodePoints(text: string, max: number): string {
	if (codePoints(text) <= max) return text;
	let out = '';
	let used = 0;
	for (const ch of characters(text)) {
		const n = codePoints(ch);
		if (used + n > max) break;
		out += ch;
		used += n;
	}
	return out;
}
