/**
 * The one-line preview shown next to a tool's name in the work log.
 *
 * A work-log row has room for exactly one short mono string, so this picks the
 * argument that actually identifies the call — the command for Bash, the path
 * for Read/Edit, the pattern for Grep — rather than dumping the first field
 * that happens to exist. Unknown tools fall back to the historical heuristic
 * field list, so nothing regresses to a bare name.
 *
 * Pure and dependency-free: everything here is unit-testable, and the row
 * component just renders what it returns.
 */
import { displayToolName } from './tool-icons';

/** Field order used when a tool has no dedicated rule. Historical behaviour. */
const FALLBACK_FIELDS = [
	'description',
	'url',
	'skill',
	'file_path',
	'path',
	'notebook_path',
	'pattern',
	'glob',
	'query',
	'command',
	'prompt',
	'text',
	'selector',
	'label',
	'script'
];

/** Per-tool preferred fields, checked in order before the fallback list. */
const TOOL_FIELDS: Record<string, string[]> = {
	Bash: ['command'],
	BashOutput: ['bash_id', 'shell_id'],
	KillShell: ['shell_id'],
	Read: ['file_path', 'path', 'notebook_path'],
	Write: ['file_path', 'path'],
	Edit: ['file_path', 'path'],
	NotebookEdit: ['notebook_path', 'file_path'],
	Grep: ['pattern'],
	Glob: ['pattern', 'glob'],
	WebFetch: ['url'],
	WebSearch: ['query'],
	Task: ['description', 'prompt'],
	Agent: ['description', 'prompt'],
	Skill: ['skill', 'name'],
	SlashCommand: ['command'],
	TodoWrite: [],
	Note: ['text'],
	AskUserQuestion: [],
	AskUser: [],
};

/** Tools whose result carries a match count worth appending to the preview. */
const COUNTED_TOOLS = new Set(['Grep', 'Glob']);

/**
 * Pull one string field out of an incomplete JSON accumulator.
 *
 * The streaming tool-input buffer is not parseable JSON until the call closes,
 * so this walks the raw text from the field's opening quote and unescapes by
 * hand. Returns '' when the field has not arrived yet.
 */
export function extractFieldFromPartial(partial: string, fieldName: string): string {
	const fieldMatch = partial.match(new RegExp(`"${fieldName}"\\s*:\\s*"`));
	if (!fieldMatch || fieldMatch.index === undefined) return '';

	let out = '';
	let i = fieldMatch.index + fieldMatch[0].length;
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
			i++;
		}
	}
	return out;
}

/** First value that reads as a useful string, for MCP tools with no known shape. */
function firstStringyArg(input: Record<string, unknown>): string {
	for (const [key, value] of Object.entries(input)) {
		if (key === 'description') continue;
		if (typeof value === 'string' && value.trim()) return value;
		if (typeof value === 'number' || typeof value === 'boolean') return String(value);
	}
	return '';
}

/** "Found 12 matches" / "Found 3 files" → " — 12 matches", when already stated. */
function matchCountSuffix(result: string | undefined): string {
	if (!result) return '';
	const head = result.slice(0, 200);
	const m = head.match(/found\s+(\d+)\s+(matches?|files?|results?)/i);
	if (m) return ` — ${m[1]} ${m[2].toLowerCase()}`;
	if (/^no (matches|files)? ?found/i.test(head.trim())) return ' — no matches';
	return '';
}

function pickFromInput(name: string, input: Record<string, unknown>): string {
	const bare = displayToolName(name);
	const preferred = TOOL_FIELDS[name] ?? TOOL_FIELDS[bare];

	if (preferred) {
		for (const field of preferred) {
			const v = input[field];
			if (typeof v === 'string' && v.trim()) return v;
			if (typeof v === 'number') return String(v);
		}
		// An explicit (possibly empty) rule still falls through to the generic
		// list rather than showing nothing — e.g. an Edit with only `old_string`.
	}

	for (const field of FALLBACK_FIELDS) {
		const v = input[field];
		if (typeof v === 'string' && v.trim()) return v;
	}

	// MCP tools have no shape we know; the first stringy argument is the best
	// available identifier and beats a bare tool name.
	if (name.startsWith('mcp__')) return firstStringyArg(input);
	return '';
}

function pickFromPartial(name: string, partial: string): string {
	const bare = displayToolName(name);
	const preferred = TOOL_FIELDS[name] ?? TOOL_FIELDS[bare] ?? [];
	for (const field of [...preferred, ...FALLBACK_FIELDS]) {
		const v = extractFieldFromPartial(partial, field);
		if (v) return v;
	}
	return '';
}

const MAX_PREVIEW = 160;

export interface ToolPreviewArgs {
	name: string;
	input?: Record<string, unknown>;
	partialInput?: string;
	result?: string;
	streaming?: boolean;
}

/** The mono preview string for a tool row. May be empty. */
export type ToolDisplayStatus = 'running' | 'complete' | 'error';

export function toolStateLabel(status: ToolDisplayStatus, dismissed = false): string {
	if (dismissed) return 'Dismissed';
	if (status === 'running') return 'Running';
	if (status === 'error') return 'Failed';
	return 'Complete';
}

function commandProgram(command: string): string | null {
	const tokens = command.trim().match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
	let index = 0;
	while (index < tokens.length) {
		const token = tokens[index]?.replace(/^['"]|['"]$/g, '') ?? '';
		if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(token)) {
			index++;
			continue;
		}
		if (token === 'sudo') {
			index++;
			while (tokens[index]?.startsWith('-')) {
				const option = tokens[index++];
				if (['-u', '-g', '-h', '-p', '-C', '-T', '-R', '--user', '--group', '--host'].includes(option)) {
					index++;
				}
			}
			continue;
		}
		if (token === 'env') {
			index++;
			while (tokens[index]?.startsWith('-')) index++;
			continue;
		}
		return token.split(/[\\/]/).at(-1) || null;
	}
	return null;
}

/** Active-call heading modelled after T3's concise "Running pnpm" row. */
export function liveToolLabel(
	name: string,
	input?: Record<string, unknown>,
	partialInput?: string
): string {
	const bare = displayToolName(name);
	const command =
		typeof input?.command === 'string'
			? input.command
			: partialInput
				? extractFieldFromPartial(partialInput, 'command')
				: '';
	if (bare === 'Bash' || bare === 'Monitor' || bare === 'SlashCommand') {
		const program = commandProgram(command);
		return program ? `Running ${program}` : 'Running command';
	}
	if (['Read', 'BashOutput', 'TaskOutput', 'TaskGet'].includes(bare)) return 'Reading';
	if (['Write', 'Edit', 'NotebookEdit'].includes(bare)) return 'Editing';
	if (['Grep', 'Glob', 'ToolSearch', 'LSP'].includes(bare)) return 'Searching code';
	if (bare === 'WebSearch') return 'Searching the web';
	if (bare === 'WebFetch') return 'Fetching page';
	// Media generation is the long-tail family: a video can run for minutes, and
	// "Using generateVideo" tells the reader nothing about the wait.
	if (['generateImage', 'generateWithReference', 'editImage'].includes(bare)) return 'Generating image';
	if (['generateVideo', 'imageToVideo', 'extendVideo'].includes(bare)) return 'Generating video';
	if (['generateAudio', 'audioToAudio'].includes(bare)) return 'Generating audio';
	if (['textTo3D', 'imageTo3D', 'retexture3D', 'rig3D', 'animate3D', 'getTask3D'].includes(bare)) return 'Building 3D model';
	if (bare === 'browser_screenshot' || bare === 'browser_screenshot_full') return 'Taking screenshot';
	if (['ScheduleWakeup', 'CronCreate', 'CronList', 'CronDelete', 'RemoteTrigger'].includes(bare)) return 'Scheduling';
	// The streaming code-mode cell renders as name="Python" (MessageArea); give
	// it a verb rather than the bare fallback.
	if (bare === 'run_python' || bare === 'Python') return 'Writing Python';
	return `Using ${bare || 'tool'}`;
}

export function toolPreview({ name, input, partialInput, result, streaming }: ToolPreviewArgs): string {
	let value = '';

	if (input && Object.keys(input).length > 0) {
		value = pickFromInput(name, input);
	} else if (partialInput) {
		try {
			const parsed = JSON.parse(partialInput) as Record<string, unknown>;
			value = pickFromInput(name, parsed);
		} catch {
			value = pickFromPartial(name, partialInput);
		}
	}

	value = value.replace(/\s+/g, ' ').trim();

	if (value && !streaming && COUNTED_TOOLS.has(displayToolName(name))) {
		value += matchCountSuffix(result);
	}

	if (value.length > MAX_PREVIEW) value = value.slice(0, MAX_PREVIEW - 1) + '…';
	return value;
}
