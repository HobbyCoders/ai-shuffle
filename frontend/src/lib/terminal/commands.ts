/**
 * Command blocks in agent messages (#1945, phase 2).
 *
 * A fenced shell block gets "Open terminal" and "Open and paste" before its
 * Copy button. The command comes from the markdown token itself, recorded
 * here under a random id when the block renders, never from the DOM: agent
 * HTML that survives the sanitizer cannot fake a block whose command differs
 * from what it shows, because it cannot know an id.
 *
 * Nothing here runs a command. "Open and paste" puts the text at the prompt
 * through the paste route, and only the owner's Enter runs it.
 */

/** Languages whose blocks are shell commands for a POSIX shell. */
const SHELL_LANGS = new Set(['sh', 'bash', 'zsh', 'shell', 'console', 'shellsession', 'shell-session', 'terminal']);
/** Session transcripts: prompt lines carry a `$ `, the rest is output. */
const TRANSCRIPT_LANGS = new Set(['console', 'shellsession', 'shell-session', 'terminal']);

export interface CommandBlock {
	/** The text to type at the prompt. Never empty. */
	command: string;
	/** Folder the fence asked for (`cwd="/path"`), or null. */
	cwd: string | null;
	/** Shell the fence asked for (`shell="bash"`), or null. */
	shell: string | null;
	/** Worker the fence named (`on="DesktopWorker"`), or null for this worker. */
	on: string | null;
	/** `target="host"`: the Unraid host rather than this worker. */
	host: boolean;
}

const KEY_RE = /\b(cwd|on|shell|target)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"']+))/g;

/** `key="value"` pairs a fence may carry after its language. */
export function fenceKeys(info: string | null | undefined): Record<string, string> {
	const out: Record<string, string> = {};
	for (const match of (info ?? '').matchAll(KEY_RE)) {
		out[match[1]] = (match[2] ?? match[3] ?? match[4] ?? '').trim();
	}
	return out;
}

/** True when the info string carries the bare word `run`, for example ```text run. */
function hasRunWord(info: string): boolean {
	return info.replace(KEY_RE, ' ').split(/\s+/).slice(1).includes('run');
}

/**
 * The command a transcript block holds. Lines that start with `$ ` are the
 * commands; everything else is output. A plain shell block whose every line
 * starts with `$ ` is treated the same way, so the prompt never gets typed.
 */
export function stripPrompts(code: string, transcript: boolean): string {
	const lines = code.replace(/\r\n?/g, '\n').split('\n');
	const filled = lines.filter((line) => line.trim() !== '');
	const prompted = filled.filter((line) => /^\s*\$ /.test(line));
	if (transcript ? prompted.length === 0 : prompted.length !== filled.length || !filled.length) {
		return transcript && prompted.length === 0 ? '' : code;
	}
	return prompted.map((line) => line.replace(/^\s*\$ /, '')).join('\n');
}

/**
 * Parse a fenced block into a runnable command, or null when it is not one.
 * `lang` is the parsed language (`parseFenceMeta`), lowercased.
 */
export function commandBlock(code: string, info: string | null | undefined, lang: string): CommandBlock | null {
	const raw = (info ?? '').trim();
	const shellLang = SHELL_LANGS.has(lang);
	if (!shellLang && !hasRunWord(raw)) return null;
	const command = stripPrompts(code, TRANSCRIPT_LANGS.has(lang)).replace(/\n+$/, '');
	if (!command.trim()) return null;
	const keys = fenceKeys(raw);
	return {
		command,
		cwd: keys.cwd || null,
		shell: keys.shell || null,
		on: keys.on || null,
		host: keys.target === 'host',
	};
}

// ── Registry ────────────────────────────────────────────────────────────
// Ids are random, so HTML an agent writes cannot point a button at a block.
// The same block rendered again (a re-render, a remount) reuses its id.

const MAX_BLOCKS = 4000;
const byId = new Map<string, CommandBlock>();
const idByContent = new Map<string, string>();

function randomId(): string {
	const bytes = new Uint8Array(12);
	globalThis.crypto.getRandomValues(bytes);
	return 'cb' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Record a block and return its id. Oldest entries go first past the cap. */
export function registerCommandBlock(block: CommandBlock, info: string): string {
	const key = `${info}\u0000${block.command}`;
	const known = idByContent.get(key);
	if (known && byId.has(known)) return known;
	const id = randomId();
	byId.set(id, block);
	idByContent.set(key, id);
	while (byId.size > MAX_BLOCKS) {
		const oldest = byId.keys().next().value as string;
		byId.delete(oldest);
	}
	while (idByContent.size > MAX_BLOCKS) {
		idByContent.delete(idByContent.keys().next().value as string);
	}
	return id;
}

/** The block a button belongs to, or null for an unknown (forged or evicted) id. */
export function lookupCommandBlock(id: string | null | undefined): CommandBlock | null {
	return (id && byId.get(id)) || null;
}

/** Test hook. */
export function clearCommandBlocks(): void {
	byId.clear();
	idByContent.clear();
}
