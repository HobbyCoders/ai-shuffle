/**
 * Terminal output leaving the emulator: as a fenced block for the composer,
 * or as a link the owner clicked.
 */

/**
 * Terminal text as a fenced block for the chat composer. Trailing spaces
 * (the emulator pads rows) and blank lines at either end go; the fence is
 * longer than any backtick run inside, so output that prints ``` stays in
 * one block. Tagged `text`, never a shell language: the transcript offers
 * Run buttons on shell blocks, and output is not a command.
 */
export function terminalSnippet(selection: string): string {
	const lines = selection
		.replace(/\r\n?/g, '\n')
		.split('\n')
		.map((line) => line.replace(/\s+$/, ''));
	while (lines.length && !lines[0]) lines.shift();
	while (lines.length && !lines[lines.length - 1]) lines.pop();
	if (!lines.length) return '';
	const body = lines.join('\n');
	const longest = Math.max(0, ...(body.match(/`+/g) ?? []).map((run) => run.length));
	const fence = '`'.repeat(Math.max(3, longest + 1));
	return `${fence}text\n${body}\n${fence}`;
}

/**
 * The address to open for a link in terminal output, or null when it is not
 * a plain web address. Program output decides what the text says, so only
 * http(s) opens: no `javascript:`, `file:` or app schemes, and no
 * credentials smuggled in front of the host.
 */
export function terminalLinkUrl(text: string): string | null {
	let url: URL;
	try {
		url = new URL(text.trim());
	} catch {
		return null;
	}
	if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
	if (url.username || url.password || !url.hostname) return null;
	return url.href;
}

/** Open a terminal link in a new browser tab, cut off from this page. */
export function openTerminalLink(text: string): boolean {
	const href = terminalLinkUrl(text);
	if (!href || typeof window === 'undefined') return false;
	window.open(href, '_blank', 'noopener,noreferrer');
	return true;
}
