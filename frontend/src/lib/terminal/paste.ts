/**
 * Paste rules for terminals (#1945). The server does the real cleaning and
 * refuses unsafe pastes; these helpers decide what the panel asks the owner
 * first and which hint it shows afterwards. Nothing here sends input.
 */

/** Text as the server will see it: LF line ends, no trailing newlines. */
export function normalizePaste(text: string): string {
	return text.replace(/\r\n?/g, '\n').replace(/\n+$/, '');
}

/**
 * Every character that could end a line somewhere: LF, a lone CR (made LF
 * above), vertical tab, form feed, NEL and the Unicode line and paragraph
 * separators. The server drops some of these instead, so counting them all
 * can only make the panel ask more often, never less.
 */
const LINE_BREAK = /[\n\x0b\x0c\u0085\u2028\u2029]/;
const TRAILING_BREAKS = /[\n\x0b\x0c\u0085\u2028\u2029]+$/;

/** The paste split into lines, for counting and the confirm preview. */
export function pasteLines(text: string): string[] {
	const clean = normalizePaste(text).replace(TRAILING_BREAKS, '');
	return clean ? clean.split(LINE_BREAK) : [];
}

/** Number of lines the paste puts at the prompt. Empty text is 0. */
export function pasteLineCount(text: string): number {
	return pasteLines(text).length;
}

/**
 * Only bash takes several lines as one paste that waits for Enter. In sh
 * each line would run as it lands, so the panel refuses there. Returns the
 * reason, or null when the paste may go ahead.
 */
export function pasteRefusal(text: string, shell: string | null | undefined): string | null {
	if (pasteLineCount(text) <= 1 || shell === 'bash') return null;
	return `${shell || 'This shell'} can take one line at a time. Paste the lines one by one, or open a bash terminal.`;
}

/** A paste of more than one line asks first. */
export function pasteNeedsConfirm(text: string): boolean {
	return pasteLineCount(text) > 1;
}

/** "Paste 6 lines?" */
export function pasteConfirmLabel(text: string): string {
	return `Paste ${pasteLineCount(text)} lines?`;
}

/**
 * Commands that deserve a second look before Enter. This only changes the
 * hint's wording; it never blocks anything, and missing a pattern is safe
 * because nothing runs without Enter anyway.
 */
const RISKY: RegExp[] = [
	/\brm\s+(?:-[a-z]*[rf][a-z]*\s+)+/i,
	/\bsudo\b/,
	/\bmkfs(?:\.\w+)?\b/,
	/\bdd\b[^\n]*\bof=/,
	/>\s*\/dev\/(?:sd|nvme|hd|disk)/,
	/\bchmod\s+(?:-R\s+)?0?777\b/,
	/\bchown\s+-R\b/,
	/\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b/,
	/\bgit\s+push\b[^\n]*(?:--force\b|-f\b)/,
	/\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f)/,
	/:\(\)\s*\{/,
	/\b(?:shutdown|reboot|poweroff|halt)\b/,
	/\bdrop\s+(?:table|database|schema)\b/i,
	/\bkill(?:all)?\s+-9\b/,
	/\bdocker\s+(?:system\s+prune|rm\s+-f|volume\s+rm)/,
];

/** True when the text matches a command that can destroy data or a machine. */
export function isRiskyCommand(text: string): boolean {
	const clean = normalizePaste(text);
	return RISKY.some((pattern) => pattern.test(clean));
}

export type PasteHint = { text: string; risky: boolean };

/** The hint under the terminal after a paste lands at the prompt. */
export function pasteHint(text: string): PasteHint {
	return isRiskyCommand(text)
		? { text: 'Check this command before you press Enter · Esc to clear', risky: true }
		: { text: 'Press Enter to run · Esc to clear', risky: false };
}

/**
 * Keys that clear what a paste put at the prompt. Bracketed pastes land in
 * readline: Ctrl-E (end of line) then Ctrl-U (kill to start) clears every
 * pasted line. Plain mode (sh) is one line, so Ctrl-U is enough.
 */
export function clearPromptKeys(mode: 'bracketed' | 'plain'): string {
	return mode === 'bracketed' ? '\x05\x15' : '\x15';
}

/**
 * Esc while a paste waits at the prompt clears it, from a keyboard or the
 * phone key bar alike. Returns the keys to send instead, or null to send
 * the input as typed.
 */
export function escClearKeys(data: string, pending: 'bracketed' | 'plain' | null): string | null {
	return pending && data === '\x1b' ? clearPromptKeys(pending) : null;
}
